"""Company systems the assistant uses for real: keys from Secret Manager, or Manne's Google sign-in.

Three ways a system gets connected (the Access tab's Connect buttons):
- "key": its key is already in Secret Manager. One Google approval by Manne lets the
  app give its own account read access to exactly those secrets (cloud_setup.py).
- "paste": no key yet. The same approval creates an empty secret the app may fill;
  Manne pastes the key in the app, and it goes straight into Secret Manager.
- "google": a Google API, used with Manne's own sign-in (the scopes are part of it).

The assistant reads chats written by many people, so any of them could try to talk
it into misusing a key. The rules that make that fail:

- Key values never reach a model. The server adds them to each request (logins such
  as SkuVault's and Amazon's are exchanged for tokens on the server), and scrubs
  them from anything a system sends back.
- Every system has one pinned address. A path can't point a request (or its key)
  at any other host.
- Reading runs straight away. Anything that changes data needs confirmed=true,
  which a model may only set after Manne agreed to that exact change.
"""

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import requests

MAX_RESPONSE_CHARS = 10000
TEAMDESK_DATABASE_ID = os.environ.get("TEAMDESK_DATABASE_ID", "56554")
READ_METHODS = {"GET", "HEAD"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class NotReady(RuntimeError):
    """The app can't read a secret this system needs."""


@dataclass
class Integration:
    id: str
    label: str
    secrets: list                 # Secret Manager names this system needs
    base: object                  # "https://host/prefix", or callable(values) -> that
    headers: object = None        # callable(values) -> dict
    auth: object = None           # callable(values) -> (user, password) for basic auth
    hint: str = ""                # useful paths and caveats, shown to the models
    probe: object = ""            # a cheap read that proves access: "/path" or ("POST", "/path", body)
    read_posts: tuple = ()        # POST paths (regex) that only read, e.g. a search
    system_id: str = ""           # its row in the Access tab
    category: str = "Other"
    kind: str = "key"             # key | paste | google (see the module docstring)
    fields: list = field(default_factory=list)  # paste: [(secret, label, placeholder, hidden)]
    help: str = ""                # paste: where to find the key, step by step
    exchange: object = None       # callable(values, http) -> {"headers"|"body": {...}, "ttl": s}: a login -> token
    services: list = field(default_factory=list)  # google: APIs to switch on in the project

    def values(self, secrets):
        out = {}
        for name in self.secrets:
            try:
                value = secrets.get(name) if secrets else None
            except Exception:  # noqa: BLE001 - no permission counts as missing
                value = None
            value = value.strip() if isinstance(value, str) else value  # a pasted line break breaks a header
            if not value:
                raise NotReady(f"the app can't read the {name} secret")
            out[name] = value
        return out

    def base_url(self, values):
        base = self.base(values) if callable(self.base) else self.base
        return base.rstrip("/")

    def paste_names(self):
        return [f[0] for f in self.fields]


# A BigCommerce API account only reaches the areas it was given (orders, products, store settings...), so the
# check tries each: any one answering proves the token. /v2/store first, because it names the storefront.
BIGCOMMERCE_PROBES = ["/v2/store", "/v3/catalog/summary", "/v2/orders?limit=1", "/v3/customers?limit=1"]


def _bigcommerce(store_hash, token_secret, label, system_id):
    return Integration(
        id=f"bigcommerce_{store_hash}", label=label, secrets=[token_secret],
        base=f"https://api.bigcommerce.com/stores/{store_hash}",
        headers=lambda v: {"X-Auth-Token": v[token_secret], "Accept": "application/json"},
        hint=("BigCommerce REST: /v2/store (store info), /v2/orders?min_date_created=YYYY-MM-DD&limit=50, "
              "/v2/orders/{id}/products, /v3/catalog/products?keyword=...&limit=50, /v3/customers?email:in=... "
              "Revenue is never summed across currencies; orders with status_id 0 (Incomplete), 5 (Cancelled) "
              "and 6 (Declined) are excluded from revenue. payment_method is free text: normalize it first."),
        probe=BIGCOMMERCE_PROBES, system_id=system_id, category="Commerce")


def _genc_base(values):
    match = re.search(r"/stores/([a-z0-9]+)", values["GENC_BIGCOMMERCE_PRODUCT_API_PATH"])
    if not match:
        raise NotReady("GENC_BIGCOMMERCE_PRODUCT_API_PATH doesn't contain a BigCommerce store path")
    return f"https://api.bigcommerce.com/stores/{match[1]}"


REGISTRY = [
    _bigcommerce("gmosz3ja", "BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN", "BigCommerce: superhairpieces.ca (CAD)",
                 "bigcommerce_ca"),
    _bigcommerce("qet21urb3p", "BIGCOMMERCE_qet21urb3p_ACCESS_TOKEN",
                 "BigCommerce store qet21urb3p (Check connections shows which storefront)", "bigcommerce_qet21urb3p"),
    Integration(
        id="bigcommerce_genc", label="BigCommerce: Gen'C Beauty",
        secrets=["GENC_BIGCOMMERCE_PRODUCT_ACCESS_TOKEN", "GENC_BIGCOMMERCE_PRODUCT_API_PATH"], base=_genc_base,
        headers=lambda v: {"X-Auth-Token": v["GENC_BIGCOMMERCE_PRODUCT_ACCESS_TOKEN"], "Accept": "application/json"},
        hint="Same BigCommerce REST paths as the other stores (/v2/store, /v2/orders, /v3/catalog/products).",
        probe=BIGCOMMERCE_PROBES, system_id="bigcommerce_genc", category="Commerce"),
    Integration(
        id="airtable", label="Airtable", secrets=["AIRTABLE_COMPANY_TOKEN"], base="https://api.airtable.com",
        headers=lambda v: {"Authorization": f"Bearer {v['AIRTABLE_COMPANY_TOKEN']}"},
        hint="/v0/meta/bases (list bases), /v0/meta/bases/{baseId}/tables, /v0/{baseId}/{tableIdOrName}?maxRecords=50",
        probe="/v0/meta/bases", system_id="airtable", category="Operations"),
    Integration(
        id="trustpilot", label="Trustpilot", secrets=["TRUSTPILOT_API_KEY"], base="https://api.trustpilot.com",
        headers=lambda v: {"apikey": v["TRUSTPILOT_API_KEY"]},
        hint=("/v1/business-units/find?name=superhairpieces.com (gives the business unit id), "
              "/v1/business-units/{id}, /v1/business-units/{id}/reviews?perPage=20&orderBy=createdat.desc"),
        probe="/v1/business-units/find?name=superhairpieces.com", system_id="trustpilot", category="Reviews"),
    Integration(
        id="stamped", label="Stamped.io", secrets=["STAMPED_STORE_HASH", "STAMPED_PUBLIC_KEY", "STAMPED_PRIVATE_KEY"],
        base=lambda v: f"https://stamped.io/api/v2/{v['STAMPED_STORE_HASH']}",
        auth=lambda v: (v["STAMPED_PUBLIC_KEY"], v["STAMPED_PRIVATE_KEY"]),
        hint="/dashboard/reviews?page=1 (product reviews), /dashboard/reviews?rating=1 (low ratings)",
        probe="/dashboard/reviews?page=1", system_id="stamped", category="Reviews"),
    Integration(
        id="omnisend", label="Omnisend", secrets=["OMNISEND_API_KEY"], base="https://api.omnisend.com",
        headers=lambda v: {"X-API-KEY": v["OMNISEND_API_KEY"], "Accept": "application/json"},
        hint="/v3/contacts?limit=50, /v3/campaigns?limit=20 (email marketing)",
        probe="/v3/contacts?limit=1", system_id="omnisend", category="Marketing"),
    Integration(
        id="notion", label="Notion", secrets=["NOTION_API_KEY"], base="https://api.notion.com",
        headers=lambda v: {"Authorization": f"Bearer {v['NOTION_API_KEY']}", "Notion-Version": "2022-06-28"},
        hint=('POST /v1/search with {"query": "..."} finds pages and databases (a read); '
              "/v1/pages/{id}, /v1/blocks/{id}/children, POST /v1/databases/{id}/query (a read)"),
        probe="/v1/users/me", read_posts=(r"^/v1/search$", r"^/v1/databases/[^/]+/query$"),
        system_id="notion", category="Documents"),
    Integration(
        id="figma", label="Figma", secrets=["FIGMA_TOKEN"], base="https://api.figma.com",
        headers=lambda v: {"X-Figma-Token": v["FIGMA_TOKEN"]},
        hint="/v1/files/{fileKey}?depth=1, /v1/files/{fileKey}/comments, /v1/images/{fileKey}?ids=...",
        probe="/v1/me", system_id="figma", category="Design"),
]


def _skuvault_tokens(values, http):
    """SkuVault's API takes a tenant and user token in every request body; they come from the stored login."""
    resp = http.request("POST", "https://app.skuvault.com/api/gettokens", timeout=30,
                        json={"Email": values["SKUVAULT_EMAIL"], "Password": values["SKUVAULT_PASSWORD"]},
                        headers={"Accept": "application/json", "Content-Type": "application/json"})
    try:
        data = json.loads(resp.text or "{}")
    except ValueError:
        data = {}
    if not data.get("TenantToken") or not data.get("UserToken"):
        raise NotReady(f"SkuVault didn't accept the stored login (SKUVAULT_EMAIL / SKUVAULT_PASSWORD, HTTP {resp.status_code})")
    return {"body": {"TenantToken": data["TenantToken"], "UserToken": data["UserToken"]}, "ttl": 12 * 3600}


def _amazon_token(values, http):
    """Selling Partner API: the stored refresh token and app credentials buy a one-hour access token."""
    resp = http.request("POST", "https://api.amazon.com/auth/o2/token", timeout=30, data={
        "grant_type": "refresh_token", "refresh_token": values["AMAZON_TOKEN"],
        "client_id": values["AMAZON_CLIENT_IDENTIFIER"], "client_secret": values["AMAZON_CLIENT_SECRET"]})
    try:
        data = json.loads(resp.text or "{}")
    except ValueError:
        data = {}
    if not data.get("access_token"):
        raise NotReady(f"Amazon didn't accept the stored app credentials (AMAZON_TOKEN / AMAZON_CLIENT_*, "
                       f"HTTP {resp.status_code}: {data.get('error_description') or data.get('error') or ''})")
    return {"headers": {"x-amz-access-token": data["access_token"]}, "ttl": int(data.get("expires_in", 3600)) - 120}


REGISTRY += [
    Integration(
        id="skuvault", label="SkuVault", secrets=["SKUVAULT_EMAIL", "SKUVAULT_PASSWORD"],
        base="https://app.skuvault.com", headers=lambda v: {"Accept": "application/json"}, exchange=_skuvault_tokens,
        hint=("Every SkuVault call is a POST with a JSON body (the server adds the tokens). Reads: "
              "/api/inventory/getWarehouses, /api/products/getProducts {\"PageNumber\": 0, \"PageSize\": 100, "
              "\"ProductSKUs\": [...]}, /api/inventory/getItemQuantities, /api/inventory/getInventoryByLocation, "
              "/api/sales/getSales, /api/purchaseorders/getPOs. Anything not named get* changes data. "
              "SkuVault rate-limits hard: ask for what you need in one call."),
        probe=("POST", "/api/inventory/getWarehouses", {}), read_posts=(r"^/api/\w+/get\w+$",),
        system_id="skuvault", category="Inventory"),
    Integration(
        id="amazon", label="Amazon Seller Central (SP-API, North America)",
        secrets=["AMAZON_TOKEN", "AMAZON_CLIENT_IDENTIFIER", "AMAZON_CLIENT_SECRET"],
        base="https://sellingpartnerapi-na.amazon.com", exchange=_amazon_token,
        headers=lambda v: {"Accept": "application/json"},
        hint=("/sellers/v1/marketplaceParticipations (marketplace ids: Canada A2EUQ1WTGCTBG2, US ATVPDKIKX0DER), "
              "/orders/v0/orders?MarketplaceIds=...&CreatedAfter=YYYY-MM-DD, /orders/v0/orders/{id}/orderItems, "
              "/fba/inventory/v1/summaries?granularityType=Marketplace&granularityId=...&marketplaceIds=..."),
        probe="/sellers/v1/marketplaceParticipations", system_id="amazon", category="Commerce"),
    Integration(
        id="hubspot", label="HubSpot", secrets=["HUBSPOT_ACCESS_TOKEN"], base="https://api.hubapi.com",
        headers=lambda v: {"Authorization": f"Bearer {v['HUBSPOT_ACCESS_TOKEN']}"}, kind="paste",
        fields=[("HUBSPOT_ACCESS_TOKEN", "Private app access token", "pat-na1-…", True)],
        help=("In HubSpot: the gear (Settings) > Integrations > Private Apps > Create a private app. Name it "
              "\"Company Assistant\". Under Scopes tick crm.objects.contacts, crm.objects.companies and "
              "crm.objects.deals (Read, and Write if the assistant may update them). Create the app, then copy "
              "the access token and paste it here."),
        hint=("/crm/v3/objects/contacts?limit=50&properties=email,firstname,lastname,hs_lead_status, "
              "/crm/v3/objects/deals?limit=50, /crm/v3/pipelines/deals, POST /crm/v3/objects/{type}/search "
              "(a read) with filterGroups"),
        probe="/crm/v3/objects/contacts?limit=1", read_posts=(r"^/crm/v3/objects/\w+/search$",),
        system_id="hubspot", category="CRM"),
    Integration(
        id="reamaze", label="Re:amaze", secrets=["REAMAZE_BRAND", "REAMAZE_EMAIL", "REAMAZE_API_TOKEN"],
        base=lambda v: f"https://{v['REAMAZE_BRAND'].strip()}.reamaze.io/api/v1",
        auth=lambda v: (v["REAMAZE_EMAIL"].strip(), v["REAMAZE_API_TOKEN"].strip()),
        headers=lambda v: {"Accept": "application/json"}, kind="paste",
        fields=[("REAMAZE_BRAND", "Brand (the part before .reamaze.io in your Re:amaze address)", "superhairpieces", False),
                ("REAMAZE_EMAIL", "The email you sign in to Re:amaze with", "manne@superhairpieces.com", False),
                ("REAMAZE_API_TOKEN", "API token", "", True)],
        help=("In Re:amaze: Settings > Developer > API Token > Generate New Token (it's tied to your login). "
              "Paste it here with your brand and sign-in email."),
        hint="/conversations?filter=open&page=1, /conversations/{slug}/messages, /contacts?q=email",
        probe="/conversations?page=1", system_id="reamaze", category="Support"),
    Integration(
        id="teamdesk", label="TeamDesk", secrets=["TEAMDESK_TOKEN"],
        # Database 56554 is the one trustpilot-pipeline writes to; TEAMDESK_DATABASE_ID (env) overrides it.
        base=lambda v: f"https://www.teamdesk.net/secure/api/v2/{TEAMDESK_DATABASE_ID}/{v['TEAMDESK_TOKEN']}",
        hint="/describe.json (tables), /{Table}/describe.json, /{Table}/select.json?column=...&filter=...&top=50",
        probe="/describe.json", system_id="teamdesk", category="Operations"),
]

REGISTRY += [  # research
    Integration(
        id="firecrawl", label="Web research (Firecrawl)", secrets=["FIRECRAWL_API"], base="https://api.firecrawl.dev",
        headers=lambda v: {"Authorization": f"Bearer {v['FIRECRAWL_API']}", "Content-Type": "application/json"},
        hint=('Used by web_search and read_webpage. Directly: POST /v1/search {"query": "...", "limit": 5}, '
              'POST /v1/scrape {"url": "...", "formats": ["markdown"], "onlyMainContent": true}, '
              'POST /v1/map {"url": "https://site"} (a site\'s pages). All are reads.'),
        probe=("POST", "/v1/search", {"query": "superhairpieces", "limit": 1}),
        read_posts=(r"^/v1/(search|scrape|map)$",), system_id="web_research", category="Research"),
    Integration(
        id="dataforseo", label="SEO research (DataForSEO)", secrets=["DATAFORSEO_LOGIN", "DATAFORSEO_API_TOKEN"],
        base="https://api.dataforseo.com", auth=lambda v: (v["DATAFORSEO_LOGIN"], v["DATAFORSEO_API_TOKEN"]),
        hint=("Each call costs a few cents: batch keywords into one request. Bodies are JSON arrays of tasks. "
              "POST /v3/serp/google/organic/live/advanced [{\"keyword\": \"...\", \"location_code\": 2124, "
              "\"language_code\": \"en\"}] (2124 Canada, 2840 US), POST /v3/keywords_data/google_ads/search_volume/live "
              "[{\"keywords\": [...], \"location_code\": 2124}], POST /v3/dataforseo_labs/google/ranked_keywords/live "
              "[{\"target\": \"superhairpieces.ca\", \"location_code\": 2124}]. GET /v3/appendix/user_data (balance)."),
        probe="/v3/appendix/user_data", read_posts=(r"^/v3/.+/live(/\w+)?$",),
        system_id="seo_research", category="Research"),
]

GOOGLE = [  # used with Manne's own Google sign-in; their scopes are in google_apis.GOOGLE_TOOL_SCOPES
    Integration(id="gmail", label="Gmail", secrets=[], base="https://gmail.googleapis.com", kind="google",
                hint=("/gmail/v1/users/me/messages?q=from:x newer_than:7d&maxResults=20, /gmail/v1/users/me/messages/{id}"
                      "?format=full. Drafts: POST /gmail/v1/users/me/drafts (a change: needs Manne's OK)."),
                probe="/gmail/v1/users/me/profile", system_id="gmail", category="Communication"),
    Integration(id="google_drive", label="Google Drive", secrets=[], base="https://www.googleapis.com", kind="google",
                hint="/drive/v3/files?q=name contains 'x'&fields=files(id,name,mimeType,modifiedTime), "
                     "/drive/v3/files/{id}/export?mimeType=text/plain (Docs as text)",
                probe="/drive/v3/about?fields=user", system_id="google_drive", category="Documents"),
    Integration(id="google_sheets", label="Google Sheets", secrets=[], base="https://sheets.googleapis.com",
                kind="google", hint="/v4/spreadsheets/{id}?fields=sheets.properties, /v4/spreadsheets/{id}/values/{range}",
                system_id="google_drive", category="Documents"),
    Integration(id="ga4_admin", label="Google Analytics (properties)", secrets=[], base="https://analyticsadmin.googleapis.com",
                kind="google", hint="/v1beta/accountSummaries (lists GA4 properties and their ids)",
                probe="/v1beta/accountSummaries", system_id="analytics", category="Marketing",
                services=["analyticsadmin.googleapis.com"]),
    Integration(id="ga4_data", label="Google Analytics (reports)", secrets=[], base="https://analyticsdata.googleapis.com",
                kind="google", read_posts=(r"^/v1beta/properties/\d+:runReport$",),
                hint=('POST /v1beta/properties/{id}:runReport (a read) with {"dateRanges": [{"startDate": "28daysAgo", '
                      '"endDate": "today"}], "dimensions": [{"name": "sessionDefaultChannelGroup"}], '
                      '"metrics": [{"name": "sessions"}, {"name": "purchaseRevenue"}]}'),
                system_id="analytics", category="Marketing", services=["analyticsdata.googleapis.com"]),
    Integration(id="search_console", label="Google Search Console", secrets=[], base="https://searchconsole.googleapis.com",
                kind="google", read_posts=(r"^/webmasters/v3/sites/[^/]+/searchAnalytics/query$",),
                hint=("/webmasters/v3/sites (the sites), POST /webmasters/v3/sites/{url-encoded site}/searchAnalytics/query "
                      '(a read) with {"startDate": "...", "endDate": "...", "dimensions": ["query"], "rowLimit": 50}'),
                probe="/webmasters/v3/sites", system_id="analytics", category="Marketing",
                services=["searchconsole.googleapis.com"]),
]
REGISTRY += GOOGLE
BY_ID = {i.id: i for i in REGISTRY}
_exchanged = {}  # integration id -> (exchange result, expires at); logins aren't redone on every call


def secret_names():
    """Every secret the integrations read: the list the app's account needs access to."""
    return sorted({name for i in REGISTRY for name in i.secrets})


def _clean_path(path):
    path = "/" + (path or "").strip().lstrip("/")
    if "://" in path or "@" in path or "\\" in path or ".." in path.split("?")[0] or re.search(r"\s", path):
        raise ValueError("give a path on this system, like /v2/orders?limit=10, not a full address")
    return path


def _scrub(text, values):
    for value in values.values():
        if len(value) >= 6:
            text = text.replace(value, "[secret]")
    return text


def is_read(integration, method, path):
    method = method.upper()
    if method in READ_METHODS:
        return True
    bare = path.split("?")[0]
    return method == "POST" and any(re.match(p, bare) for p in integration.read_posts)


def _exchange(integration, values, http):
    import time

    hit = _exchanged.get(integration.id)
    if hit and hit[1] > time.time():
        return hit[0]
    result = integration.exchange(values, http)
    _exchanged[integration.id] = (result, time.time() + result.get("ttl", 3600))
    return result


def call(integration_id, method, path, secrets, query=None, body=None, confirmed=False, session=None, google=None):
    """One request to a company system; returns a JSON-safe dict for the model (never a key)."""
    integration = BY_ID.get(integration_id)
    if not integration:
        return {"error": f"unknown system {integration_id}; call list_integrations for the ids"}
    method = (method or "GET").upper()
    if method not in READ_METHODS | WRITE_METHODS:
        return {"error": f"method must be one of {sorted(READ_METHODS | WRITE_METHODS)}"}
    try:
        path = _clean_path(path)
    except ValueError as exc:
        return {"error": str(exc)}
    if not is_read(integration, method, path) and not confirmed:
        return {"not_sent": True,
                "reason": "This changes data. Describe the exact change to Manne and call again with confirmed=true "
                          "only after he agrees to it."}
    http = session or requests
    try:
        values = integration.values(secrets)
        base = integration.base_url(values)
        headers = dict(integration.headers(values)) if integration.headers else {}
        if integration.kind == "google":
            if google is None:
                raise NotReady("Google isn't connected (Connect Google on the Talk tab)")
            headers.update(google.auth_header())
        extra = _exchange(integration, values, http) if integration.exchange else {}
    except NotReady as exc:
        return {"error": f"{integration.label} isn't available: {exc}"}
    except Exception as exc:  # noqa: BLE001 - a failed login or token refresh, reported to the model
        return {"error": f"{integration.label} isn't available: {str(exc)[:300]}"}
    scrub = dict(values, **{f"x{i}": str(v) for i, v in enumerate({**extra.get("headers", {}),
                                                                   **extra.get("body", {})}.values())})
    headers.update(extra.get("headers", {}))
    if extra.get("body"):
        body = {**(body or {}), **extra["body"]}
    url = base + path
    if query:
        url += ("&" if "?" in url else "?") + urlencode(query, doseq=True)
    if urlsplit(url).netloc != urlsplit(base).netloc:  # belt and braces: the key only ever goes to its own host
        return {"error": "that path leaves the system's own address"}
    auth = integration.auth(values) if integration.auth else None
    try:
        resp = http.request(method, url, headers=headers, auth=auth, json=body if body is not None else None, timeout=45)
    except requests.RequestException as exc:
        return {"error": _scrub(f"{integration.label} didn't answer: {exc}", scrub)}
    if resp.status_code == 401 and integration.exchange:
        _exchanged.pop(integration.id, None)  # a stale token: log in again next time
    text = _scrub(resp.text or "", scrub)
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = text
    out = {"system": integration.label, "status": resp.status_code, "ok": resp.ok}
    rendered = json.dumps(data, ensure_ascii=False, default=str)
    if len(rendered) > MAX_RESPONSE_CHARS:
        out.update(truncated=True, data=rendered[:MAX_RESPONSE_CHARS],
                   note="Response cut short; narrow it with filters, limit or paging.")
    else:
        out["data"] = data
    return out


def probe(integration, secrets, session=None, google=None):
    """The system's cheap read(s). With several, the first that answers wins; else the most telling failure."""
    probes = integration.probe if isinstance(integration.probe, list) else [integration.probe]
    first = None
    for one in probes:
        method, path, body = ("GET", one, None) if isinstance(one, str) else one
        result = call(integration.id, method, path, secrets, body=body, session=session, google=google)
        if result.get("ok") or result.get("error"):  # answered, or not reachable at all: no point trying more
            return result
        first = first or result
        if result.get("status") == 401:  # the key itself was refused: other paths won't help
            return result
    return first


def status(integration, secrets, google=None):
    if integration.kind == "google":
        return ("ready", "") if google is not None else ("no_access", "Google isn't connected")
    try:
        integration.values(secrets)
        return "ready", ""
    except NotReady as exc:
        return "no_access", str(exc)


def describe(secrets, google=None):
    """What the models see in list_integrations: ids, whether usable, how to use them. No values."""
    out = []
    for i in REGISTRY:
        state, why = status(i, secrets, google)
        out.append({"id": i.id, "system": i.label, "status": state, **({"why": why} if why else {}),
                    "how": i.hint, "changes_need_confirmation": True})
    return out


def connect_info(system_id):
    """How the Access tab connects this system (None: no connector yet, ask the assistant)."""
    found = [i for i in REGISTRY if i.system_id == system_id]
    if not found:
        return None
    paste = next((i for i in found if i.kind == "paste"), None)
    if paste:
        return {"kind": "paste", "integration": paste.id, "help": paste.help,
                "fields": [{"secret": n, "label": label, "placeholder": ph, "hidden": hidden}
                           for n, label, ph, hidden in paste.fields]}
    return {"kind": found[0].kind, "integration": found[0].id}


def seed_systems(store):
    """Give every connector a row in the Access tab (existing rows keep their status)."""
    for i in REGISTRY:
        if i.system_id and not store.get_item("systems", i.system_id):
            store.save_item("systems", i.system_id, {
                "name": i.label, "category": i.category, "status": "available" if i.kind == "key" else "needed",
                "unlocks": i.hint[:200]})


def check_all(secrets, store=None, session=None, google=None, only=None):
    """Run each system's cheap read (all, or the ids in `only`). Updates the Access tab when a store is given."""
    results, by_system = {}, {}
    for i in REGISTRY:
        if not i.probe or (only and i.id not in only):
            continue
        result = probe(i, secrets, session=session, google=google)
        ok = bool(result.get("ok"))
        detail = result.get("error") or ("" if ok else f"HTTP {result.get('status')}: {str(result.get('data'))[:200]}")
        label = i.label
        data = result.get("data") if ok else None
        if ok and i.id.startswith("bigcommerce") and isinstance(data, dict) and data.get("domain"):
            label = f"BigCommerce: {data['domain']} ({data.get('currency', '?')})"  # names the unknown stores
        results[i.id] = {"ok": ok, "system": label, "detail": detail}
        if i.system_id:
            by_system.setdefault(i.system_id, []).append((i, ok, label, detail))
    for system_id, rows in (by_system.items() if store is not None else ()):
        ok = all(r[1] for r in rows)  # a system with several parts (Analytics) is connected when all work
        first_bad = next((r for r in rows if not r[1]), rows[0])
        detail = "" if ok else first_bad[3]
        state = "connected" if ok else ("no_access" if "can't read" in detail or "isn't connected" in detail
                                        else "error")
        existing = store.get_item("systems", system_id) or {}
        label = rows[0][2] if len(rows) == 1 else existing.get("name", rows[0][0].label)
        store.save_item("systems", system_id, {
            "name": label if ok else existing.get("name", label), "category": existing.get("category", rows[0][0].category),
            "status": state, "why": "" if ok else detail[:300],
            "unlocks": existing.get("unlocks") or rows[0][0].hint[:200], "via": rows[0][0].kind})
    return results
