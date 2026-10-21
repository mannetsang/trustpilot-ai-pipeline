"""
Store registry and credential resolution for the Bing indexing jobs.

Six BigCommerce storefronts, one per market. Each is a separate store with its
own hash and its own access token, so every job loops over this registry rather
than assuming a single store.

Canonical hosts matter here: Bing matches submitted URLs against the verified
site exactly, and a URL that 301s is a wasted submission. .com canonicalises to
www; the other five are non-www. Verified 2026-10-20 by following the redirect
on /xmlsitemap.php for each domain.
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
    token_env: str      # env var in the project-root .env

    @property
    def api_base(self) -> str:
        return f"https://api.bigcommerce.com/stores/{self.hash}"

    @property
    def sitemap_url(self) -> str:
        return f"{self.origin}/xmlsitemap.php"

    @property
    def site_url(self) -> str:
        """The property as Bing Webmaster Tools knows it."""
        return self.origin + "/"

    def token(self) -> str:
        return get_secret(
            f"BIGCOMMERCE_{self.hash}_ACCESS_TOKEN",
            env_var=self.token_env,
        )


# ES is the odd one out: its .env keys use the _PRODUCT_ naming, not the hash.
STORES = [
    Store("ca",  "gmosz3ja",   "https://superhairpieces.ca",      "CA_BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN"),
    Store("com", "cavofu",     "https://www.superhairpieces.com", "COM_BIGCOMMERCE_cavofu_ACCESS_TOKEN"),
    Store("nl",  "1f8t0plkkw", "https://superhairpieces.nl",      "NL_BIGCOMMERCE_1f8t0plkkw_ACCESS_TOKEN"),
    Store("fr",  "1tqjsol232", "https://superhairpieces.fr",      "FR_BIGCOMMERCE_1tqjsol232_ACCESS_TOKEN"),
    Store("es",  "qet21urb3p", "https://superhairpieces.es",      "ES_BIGCOMMERCE_PRODUCT_ACCESS_TOKEN"),
    Store("de",  "34amlu9gm",  "https://superhairpieces.de",      "DE_BIGCOMMERCE_34amlu9gm_ACCESS_TOKEN"),
]

BY_KEY = {s.key: s for s in STORES}


def select(keys: list[str] | None) -> list[Store]:
    """Resolve --store arguments to Store objects; no argument means all six."""
    if not keys:
        return list(STORES)
    out = []
    for k in keys:
        k = k.strip().lower()
        if k not in BY_KEY:
            raise SystemExit(f"unknown store '{k}'; known: {', '.join(BY_KEY)}")
        out.append(BY_KEY[k])
    return out


def bing_api_key() -> str:
    """Bing Webmaster Tools API key.

    Generated once in the BWT portal under Settings -> API access -> API key.
    One key covers every site on the account, so all six stores share it.
    """
    try:
        return get_secret("BING_WEBMASTER_API_KEY", env_var="BING_WEBMASTER_API_KEY")
    except SecretNotFound:
        raise SystemExit(
            """No Bing Webmaster API key.
  Generate one at https://www.bing.com/webmasters -> Settings -> API access,
  then either put BING_WEBMASTER_API_KEY=<key> in the project-root .env,
  or add it to Secret Manager as BING_WEBMASTER_API_KEY.
  See bing-indexing/README.md."""
        ) from None


STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
