"""
The six storefronts, in one place.

Every market is a separate BigCommerce store with its own hash, its own token
and its own currency, so anything that reports on "the business" has to loop
over this list and keep the currencies apart. Revenue is never summed across
them.

Canonical hosts matter wherever a URL is matched against a verified property or
submitted to a search engine: `.com` canonicalises to **www**, the other five
are non-www. Verified 2026-10-20 by following the redirect on /xmlsitemap.php.

`bing-indexing/config.py`, `google-merchant/merchant_api.py` and
`google-search-console/config.py` each carry their own copy of this list, from
before there was a shared one. New code should use this module; those three are
worth folding in next time one of them is touched.
"""
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.secrets import SecretNotFound, get_secret  # noqa: E402


@dataclass(frozen=True)
class Store:
    key: str            # short market code used on the command line
    hash: str           # BigCommerce store hash
    origin: str         # canonical scheme+host, no trailing slash
    currency: str       # the only currency this store's revenue may be stated in
    market: str         # ISO country this storefront sells to
    language: str
    feed_label: str     # Merchant Center feed label
    token_env: str      # env var in the project-root .env
    token_secret: str   # Secret Manager id

    @property
    def api_base(self) -> str:
        return f"https://api.bigcommerce.com/stores/{self.hash}"

    @property
    def site_url(self) -> str:
        """The property as Search Console and Bing know it: trailing slash."""
        return self.origin + "/"

    @property
    def domain_property(self) -> str:
        """The same site as a Search Console Domain property."""
        return "sc-domain:" + self.origin.split("://", 1)[1].removeprefix("www.")

    @property
    def sitemap_url(self) -> str:
        return f"{self.origin}/xmlsitemap.php"

    @property
    def domain(self) -> str:
        return self.origin.split("://", 1)[1]

    def token(self):
        """The store's API token, or None when it is not stored.

        Returns None rather than raising: a dashboard that covers six stores
        should render the five it can reach and say what is wrong with the
        sixth.
        """
        for secret_id, env_var in ((self.token_secret, self.token_env),):
            try:
                return _clean_token(get_secret(secret_id, env_var=env_var))
            except SecretNotFound:
                return None


def _clean_token(raw):
    """Take the token out of a secret that holds a whole pasted credential block.

    BIGCOMMERCE_qet21urb3p_ACCESS_TOKEN holds the entire panel printout -
    "ACCESS TOKEN: ... CLIENT NAME: ... CLIENT SECRET: ..." on CR-separated
    lines - rather than the bare token. An HTTP header built from that raises
    ValueError and puts the client secret in the traceback, so pull the token
    out here and never let the blob reach a request. The secret itself still
    wants fixing; see docs/credentials.md.
    """
    if not raw:
        return None
    text = raw.strip()
    if "ACCESS TOKEN" in text.upper() or "\r" in text or "\n" in text:
        import re
        match = re.search(r"ACCESS[ _]TOKEN\s*:\s*([A-Za-z0-9._-]+)", text, re.I)
        text = match.group(1) if match else text.splitlines()[0].strip()
    return text or None


STORES = [
    Store("ca",  "gmosz3ja",   "https://superhairpieces.ca",      "CAD", "CA", "en", "CA",
          "CA_BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN", "BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN"),
    Store("com", "cavofu",     "https://www.superhairpieces.com", "USD", "US", "en", "US",
          "COM_BIGCOMMERCE_cavofu_ACCESS_TOKEN", "BIGCOMMERCE_cavofu_ACCESS_TOKEN"),
    Store("nl",  "1f8t0plkkw", "https://superhairpieces.nl",      "EUR", "NL", "nl", "NL",
          "NL_BIGCOMMERCE_1f8t0plkkw_ACCESS_TOKEN", "BIGCOMMERCE_1f8t0plkkw_ACCESS_TOKEN"),
    Store("fr",  "1tqjsol232", "https://superhairpieces.fr",      "EUR", "FR", "fr", "FR",
          "FR_BIGCOMMERCE_1tqjsol232_ACCESS_TOKEN", "BIGCOMMERCE_1tqjsol232_ACCESS_TOKEN"),
    Store("es",  "qet21urb3p", "https://superhairpieces.es",      "EUR", "ES", "es", "ES",
          "ES_BIGCOMMERCE_PRODUCT_ACCESS_TOKEN", "BIGCOMMERCE_qet21urb3p_ACCESS_TOKEN"),
    Store("de",  "34amlu9gm",  "https://superhairpieces.de",      "EUR", "DE", "de", "DE",
          "DE_BIGCOMMERCE_34amlu9gm_ACCESS_TOKEN", "BIGCOMMERCE_34amlu9gm_ACCESS_TOKEN"),
]

BY_KEY = {s.key: s for s in STORES}


def select(keys=None):
    """Resolve market keys to Store objects; no argument means all six."""
    if not keys:
        return list(STORES)
    out = []
    for key in keys:
        key = key.strip().lower()
        if key not in BY_KEY:
            raise SystemExit(f"unknown store '{key}'; known: {', '.join(BY_KEY)}")
        out.append(BY_KEY[key])
    return out


# BigCommerce order statuses. Incomplete, Cancelled, Declined and Refunded are
# not revenue; see CLAUDE.md.
ORDER_STATUS = {
    0: "Incomplete", 1: "Pending", 2: "Shipped", 3: "Partially Shipped", 4: "Refunded",
    5: "Cancelled", 6: "Declined", 7: "Awaiting Payment", 8: "Awaiting Pickup",
    9: "Awaiting Shipment", 10: "Completed", 11: "Awaiting Fulfillment",
    12: "Manual Verification Required", 13: "Disputed", 14: "Partially Refunded",
}
NON_REVENUE_STATUS = {0, 5, 6}
