"""Company systems the assistant can use with credentials already in Secret Manager.

The assistant reads chats written by many people, so any of them could try to talk
it into misusing a key. The rules that make that fail:

- Secret values never reach a model. The server adds them to each request, and
  scrubs them from anything a system sends back.
- Every system has one pinned address. A path can't point a request (or its key)
  at any other host.
- Reading runs straight away. Anything that changes data needs confirmed=true,
  which a model may only set after Manne agreed to that exact change.

Only systems with a plain API key or token are wired up here. Logins (SkuVault,
Walmart, databases) and OAuth flows (Amazon) need their own handling.
"""

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import requests

MAX_RESPONSE_CHARS = 10000
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
    probe: str = ""               # a cheap read that proves the key works
    read_posts: tuple = ()        # POST paths (regex) that only read, e.g. a search
    system_id: str = ""           # its row in the Access tab
    category: str = "Other"

    def values(self, secrets):
        out = {}
        for name in self.secrets:
            try:
                value = secrets.get(name) if secrets else None
            except Exception:  # noqa: BLE001 - no permission counts as missing
                value = None
            if not value:
                raise NotReady(f"the app can't read the {name} secret")
            out[name] = value
        return out

    def base_url(self, values):
        base = self.base(values) if callable(self.base) else self.base
        return base.rstrip("/")


def _bigcommerce(store_hash, token_secret, label, system_id):
    return Integration(
        id=f"bigcommerce_{store_hash}", label=label, secrets=[token_secret],
        base=f"https://api.bigcommerce.com/stores/{store_hash}",
        headers=lambda v: {"X-Auth-Token": v[token_secret], "Accept": "application/json"},
        hint=("BigCommerce REST: /v2/store (store info), /v2/orders?min_date_created=YYYY-MM-DD&limit=50, "
              "/v2/orders/{id}/products, /v3/catalog/products?keyword=...&limit=50, /v3/customers?email:in=... "
              "Revenue is never summed across currencies; orders with status_id 0 (Incomplete), 5 (Cancelled) "
              "and 6 (Declined) are excluded from revenue. payment_method is free text: normalize it first."),
        probe="/v2/store", system_id=system_id, category="Commerce")


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
        probe="/v2/store", system_id="bigcommerce_genc", category="Commerce"),
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
BY_ID = {i.id: i for i in REGISTRY}


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


def call(integration_id, method, path, secrets, query=None, body=None, confirmed=False, session=None):
    """One request to a company system; returns a JSON-safe dict for the model (never the key)."""
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
    try:
        values = integration.values(secrets)
        base = integration.base_url(values)
    except NotReady as exc:
        return {"error": f"{integration.label} isn't available: {exc}"}
    url = base + path
    if query:
        url += ("&" if "?" in url else "?") + urlencode(query, doseq=True)
    if urlsplit(url).netloc != urlsplit(base).netloc:  # belt and braces: the key only ever goes to its own host
        return {"error": "that path leaves the system's own address"}
    headers = dict(integration.headers(values)) if integration.headers else {}
    auth = integration.auth(values) if integration.auth else None
    try:
        resp = (session or requests).request(method, url, headers=headers, auth=auth,
                                             json=body if body is not None else None, timeout=45)
    except requests.RequestException as exc:
        return {"error": _scrub(f"{integration.label} didn't answer: {exc}", values)}
    text = _scrub(resp.text or "", values)
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


def status(integration, secrets):
    try:
        integration.values(secrets)
        return "ready", ""
    except NotReady as exc:
        return "no_access", str(exc)


def describe(secrets):
    """What the models see in list_integrations: ids, whether usable, how to use them. No values."""
    out = []
    for i in REGISTRY:
        state, why = status(i, secrets)
        out.append({"id": i.id, "system": i.label, "status": state, **({"why": why} if why else {}),
                    "how": i.hint, "changes_need_confirmation": True})
    return out


def check_all(secrets, store=None, session=None):
    """Run each system's cheap read. Updates the Access tab when a store is given. Returns per-system results."""
    results = {}
    for i in REGISTRY:
        result = call(i.id, "GET", i.probe, secrets, session=session)
        ok = bool(result.get("ok"))
        detail = result.get("error") or ("" if ok else f"HTTP {result.get('status')}")
        label = i.label
        data = result.get("data") if ok else None
        if ok and i.id.startswith("bigcommerce") and isinstance(data, dict) and data.get("domain"):
            label = f"BigCommerce: {data['domain']} ({data.get('currency', '?')})"  # names the unknown stores
        results[i.id] = {"ok": ok, "system": label, "detail": detail}
        if store is not None and i.system_id:
            state = "connected" if ok else ("no_access" if "can't read" in detail else "error")
            existing = store.get_item("systems", i.system_id) or {}
            store.save_item("systems", i.system_id, {
                "name": label if ok else existing.get("name", label), "category": existing.get("category", i.category),
                "status": state, "why": "" if ok else detail[:300],
                "unlocks": existing.get("unlocks") or i.hint[:200], "via": "Secret Manager"})
    return results
