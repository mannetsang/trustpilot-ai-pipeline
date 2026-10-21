"""
Property registry and credential resolution for the Search Console jobs.

Six storefronts, six Search Console properties. A property's identity string is
exact and goes in the request *path*: `https://superhairpieces.ca/` with the
trailing slash, and `.com` is the **www** host because that is what it
canonicalises to. Getting either wrong returns 404, not an empty result.

Canonical hosts are the same ones `bing-indexing/config.py` verified on
2026-10-20 by following the redirect on /xmlsitemap.php; keep the two in step.

Search Console grants access per property and has no org-wide role, so whatever
credential is used has to be added to all six.
"""
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.secrets import SecretNotFound, get_secret  # noqa: E402


@dataclass(frozen=True)
class Property:
    key: str        # short market code used on the command line
    origin: str     # canonical scheme+host, no trailing slash
    market: str     # the country this storefront sells to
    language: str   # content language, for URL inspection

    @property
    def site_url(self) -> str:
        """The property as Search Console knows it: URL-prefix, trailing slash."""
        return self.origin + "/"

    @property
    def domain_property(self) -> str:
        """The same site as a Domain property, if one is verified instead."""
        return "sc-domain:" + self.origin.split("://", 1)[1].removeprefix("www.")

    @property
    def sitemap_url(self) -> str:
        return f"{self.origin}/xmlsitemap.php"


PROPERTIES = [
    Property("ca",  "https://superhairpieces.ca",      "CA", "en"),
    Property("com", "https://www.superhairpieces.com", "US", "en"),
    Property("nl",  "https://superhairpieces.nl",      "NL", "nl"),
    Property("fr",  "https://superhairpieces.fr",      "FR", "fr"),
    Property("es",  "https://superhairpieces.es",      "ES", "es"),
    Property("de",  "https://superhairpieces.de",      "DE", "de"),
]

BY_KEY = {p.key: p for p in PROPERTIES}


def select(keys):
    """Resolve --property arguments to Property objects; no argument means all six."""
    if not keys:
        return list(PROPERTIES)
    out = []
    for key in keys:
        key = key.strip().lower()
        if key not in BY_KEY:
            raise SystemExit(f"unknown property '{key}'; known: {', '.join(BY_KEY)}")
        out.append(BY_KEY[key])
    return out


# --------------------------------------------------------------------------- credentials

# Read-only is all any job here needs. `.../auth/webmasters` would also allow
# submitting and deleting sitemaps; bing-indexing's opposite number does that
# for Bing, and nothing here should be able to change Google's view of the site.
SCOPE_READONLY = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPE_WRITE = "https://www.googleapis.com/auth/webmasters"

# A user OAuth triple, like google-business/. The three values travel together
# because a refresh token can only be exchanged by the client that issued it.
CLIENT_ID_SECRET = "google-search-console-client-id"
CLIENT_SECRET_SECRET = "google-search-console-client-secret"
REFRESH_TOKEN_SECRET = "google-search-console-refresh-token"

# Minting the refresh token against the Business Profile OAuth client is fine
# and leaves that token working - Google allows many refresh tokens per user and
# client - so the client id and secret fall back to it when only the refresh
# token has been stored. What is *not* fine is reusing the Business Profile
# refresh token itself: it carries business.manage only, the token call still
# returns 200, and every Search Console request after it fails 403
# insufficientPermissions. verify_scope() below refuses that token at the door.
FALLBACK_CLIENT_ID_SECRET = "google-business-profile-client-id"
FALLBACK_CLIENT_SECRET_SECRET = "google-business-profile-client-secret"


def oauth_triple():
    """(client_id, client_secret, refresh_token), or None when none is stored."""
    try:
        refresh_token = get_secret(REFRESH_TOKEN_SECRET, env_var="GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN")
    except SecretNotFound:
        return None
    client_id = _first(
        (CLIENT_ID_SECRET, "GOOGLE_SEARCH_CONSOLE_CLIENT_ID"),
        (FALLBACK_CLIENT_ID_SECRET, "GOOGLE_BUSINESS_PROFILE_CLIENT_ID"),
    )
    client_secret = _first(
        (CLIENT_SECRET_SECRET, "GOOGLE_SEARCH_CONSOLE_CLIENT_SECRET"),
        (FALLBACK_CLIENT_SECRET_SECRET, "GOOGLE_BUSINESS_PROFILE_CLIENT_SECRET"),
    )
    if not client_id or not client_secret:
        raise SystemExit(
            f"{REFRESH_TOKEN_SECRET} is stored but its OAuth client is not. Add "
            f"{CLIENT_ID_SECRET} and {CLIENT_SECRET_SECRET} (or reuse the "
            "google-business-profile client, which issued it)."
        )
    return client_id, client_secret, refresh_token


def _first(*candidates):
    for secret_id, env_var in candidates:
        try:
            return get_secret(secret_id, env_var=env_var)
        except SecretNotFound:
            continue
    return None
