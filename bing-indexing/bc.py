"""
BigCommerce side: which URLs changed since we last pinged Bing.

Products carry date_modified and the v3 catalog endpoint filters on it, so a
watermark poll catches every price, stock and new-product change regardless of
what caused it - a manual edit in the control panel, an app, or one of our own
sync jobs. That is deliberately broader than hooking a single sync.

Only visible products are returned: submitting a hidden URL wastes quota and
teaches Bing nothing.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 250
TIMEOUT = 45


class BigCommerceError(RuntimeError):
    pass


def _get(store, token, path, params):
    url = f"{store.api_base}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise BigCommerceError(f"{store.key}: {path} -> HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise BigCommerceError(f"{store.key}: {path} unreachable: {exc.reason}") from None


def _abs(store, path: str) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return store.origin + "/" + path.lstrip("/")


def changed_product_urls(store, token, since_iso: str | None, limit: int | None = None):
    """Canonical URLs of visible products modified at or after since_iso.

    since_iso of None means every visible product (first-run seeding).
    """
    params = {
        "limit": PAGE_SIZE,
        "include_fields": "name,custom_url,is_visible,date_modified",
    }
    if since_iso:
        params["date_modified:min"] = since_iso

    urls, seen, page = [], set(), 1
    while True:
        body = _get(store, token, "/v3/catalog/products", {**params, "page": page})
        data = body.get("data", [])
        for p in data:
            if not p.get("is_visible"):
                continue
            u = _abs(store, (p.get("custom_url") or {}).get("url", ""))
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
        if limit and len(urls) >= limit:
            return urls[:limit]
        pagination = body.get("meta", {}).get("pagination", {})
        if page >= pagination.get("total_pages", page) or not data:
            break
        page += 1
        time.sleep(0.15)  # the catalog API rate-limits on burst
    return urls
