"""Taking a consumption line's stock out of SkuVault.

The dashboard removes stock with SkuVault's inventory/removeItem (one call per bin), after looking
up where the SKU sits with inventory/getInventoryByLocation. Bins are used least stock first, in the
allowed warehouses only (SKUVAULT_WAREHOUSES), spilling into the next bin when one bin is not
enough. The transaction reason is the English name of the dashboard's reason (SKUVAULT_REASONS); a
reason must already exist in SkuVault or the call is refused.

SkuVault throttles per tenant, shared with every other integration: getInventoryByLocation is
"Heavy" (5 calls a minute), removeItem "Moderate" (10 a minute). A 429 is retried once when its
reset is a few seconds away. Tokens come from getTokens with the SKUVAULT_EMAIL / SKUVAULT_PASSWORD
login and are kept for the life of the instance.
"""

import threading
import time

import requests

API = "https://app.skuvault.com/api"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
MAX_THROTTLE_WAIT = 20          # seconds: a longer wait fails the call instead of holding the page


class SkuVaultError(Exception):
    pass


class NotEnoughStock(SkuVaultError):
    pass


def plan_removal(bins, qty):
    """Bins [{warehouse_code, warehouse_id, location_code, quantity}] -> [(bin, qty to take)],
    least stock first. Raises NotEnoughStock when the bins together hold less than qty."""
    usable = sorted((b for b in bins if b["quantity"] > 0),
                    key=lambda b: (b["quantity"], b["warehouse_code"], b["location_code"]))
    available = sum(b["quantity"] for b in usable)
    if available < qty:
        where = ", ".join(f"{b['warehouse_code']} {b['location_code']} ({b['quantity']})" for b in usable) or "no bin"
        raise NotEnoughStock(f"only {available} on hand in the allowed warehouses ({where}); {qty} needed")
    plan, left = [], qty
    for b in usable:
        if left <= 0:
            break
        take = min(left, b["quantity"])
        plan.append((b, take))
        left -= take
    return plan


class SkuVaultClient:
    def __init__(self, email, password, warehouses=("WH1",), timeout=30, session=None):
        if not email or not password:
            raise SkuVaultError("SkuVault is not configured (SKUVAULT_EMAIL / SKUVAULT_PASSWORD)")
        self.email, self.password = email, password
        self.warehouses = [w.upper() for w in warehouses]
        self.timeout = timeout
        self.http = session or requests.Session()
        self._lock = threading.Lock()
        self._tokens = None
        self._warehouse_ids = None

    # -- plumbing ---------------------------------------------------------------------------
    def _post(self, path, body, auth=True, retried=False):
        payload = dict(body)
        if auth:
            payload.update(self.tokens())
        try:
            response = self.http.post(f"{API}/{path}", json=payload, headers=HEADERS, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SkuVaultError(f"SkuVault did not answer ({exc.__class__.__name__})") from exc
        if response.status_code == 429 and not retried:
            wait = _reset_seconds(response)
            if wait is not None and wait <= MAX_THROTTLE_WAIT:
                time.sleep(wait + 0.5)
                return self._post(path, body, auth, retried=True)
            raise SkuVaultError("SkuVault is busy (rate limit shared with the other integrations); try again in a minute")
        if response.status_code == 401 and auth and not retried:
            with self._lock:
                self._tokens = None
            return self._post(path, body, auth, retried=True)
        if response.status_code >= 300:
            raise SkuVaultError(f"SkuVault answered {response.status_code}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError:
            raise SkuVaultError(f"SkuVault answered with something that is not JSON: {response.text[:120]}") from None

    def tokens(self):
        with self._lock:
            if self._tokens:
                return self._tokens
        data = self._post("gettokens", {"Email": self.email, "Password": self.password}, auth=False)
        if not data.get("TenantToken") or not data.get("UserToken"):
            raise SkuVaultError("SkuVault refused the login (getTokens returned no tokens)")
        with self._lock:
            self._tokens = {"TenantToken": data["TenantToken"], "UserToken": data["UserToken"]}
            return self._tokens

    def warehouse_ids(self):
        with self._lock:
            if self._warehouse_ids is not None:
                return self._warehouse_ids
        data = self._post("inventory/getWarehouses", {"PageNumber": 0})
        ids = {str(w["Code"]).upper(): int(w["Id"]) for w in data.get("Warehouses", [])}
        with self._lock:
            self._warehouse_ids = ids
        return ids

    # -- reads --------------------------------------------------------------------------------
    def bins(self, skus):
        """{sku as asked: [bin, ...]} for the allowed warehouses; one Heavy call for all the SKUs."""
        skus = sorted(set(skus))
        data = self._post("inventory/getInventoryByLocation",
                          {"ProductSKUs": skus, "IsReturnByCodes": False, "PageNumber": 0, "PageSize": 1000})
        if data.get("Errors"):
            raise SkuVaultError(f"SkuVault: {'; '.join(map(str, data['Errors']))[:200]}")
        ids = self.warehouse_ids()
        by_lower = {k.lower(): v for k, v in (data.get("Items") or {}).items()}
        out = {}
        for sku in skus:
            out[sku] = [{"warehouse_code": str(loc["WarehouseCode"]).upper(), "warehouse_id": ids.get(str(loc["WarehouseCode"]).upper()),
                         "location_code": loc["LocationCode"], "quantity": int(loc.get("Quantity") or 0)}
                        for loc in by_lower.get(sku.lower(), [])
                        if str(loc["WarehouseCode"]).upper() in self.warehouses and not loc.get("Reserve")]
        return out

    # -- writes -------------------------------------------------------------------------------
    def remove(self, sku, warehouse_id, location_code, qty, reason):
        data = self._post("inventory/removeItem", {"Sku": sku, "WarehouseId": warehouse_id, "LocationCode": location_code,
                                                   "Quantity": qty, "Reason": reason})
        status = data.get("RemoveItemStatus")
        if status != "Success":
            detail = status or "; ".join(map(str, data.get("Errors") or [])) or str(data)[:160]
            raise SkuVaultError(f"SkuVault refused the removal from {location_code}: {detail}")


def _reset_seconds(response):
    raw = response.headers.get("X-RateLimit-Reset")
    try:
        return float(raw) / 1000.0
    except (TypeError, ValueError):
        return None
