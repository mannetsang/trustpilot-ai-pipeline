"""Shared helpers for the Google Merchant API scripts in this folder.

Authentication is Application Default Credentials in every environment:

- GitHub Actions: the GCP_SA_KEY service account (google-github-actions/auth).
- Claude cloud sessions: GOOGLE_APPLICATION_CREDENTIALS written by the hook.
- Your machine: `gcloud auth application-default login`.

Whichever identity that resolves to must also be a user (Admin) on the
Merchant Center account: Settings -> People and access. No API key, no OAuth
client secret, and therefore nothing for Secret Manager to hold.

The Merchant Center account id is not a secret. It comes from --account or
the GMC_ACCOUNT_ID environment variable (a gitignored .env works locally).
"""

import argparse
import os
import sys

# Keep imports for lib/ working whether we're run from the repo root or here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.secrets import load_dotenv  # noqa: E402

GCP_PROJECT = "shp-ai-bot-2026"

# One primary product data source per storefront. Merchant Center requires a
# separate data source for every (feed label, content language) pair, so the
# six storefronts cannot share one. Feed labels equal the country code, which
# is what Shopping campaigns filter on.
STOREFRONTS = [
    # (storefront, feed_label, content_language, countries)
    ("superhairpieces.ca", "CA", "en", ["CA"]),
    ("superhairpieces.com", "US", "en", ["US"]),
    ("superhairpieces.nl", "NL", "nl", ["NL"]),
    ("superhairpieces.fr", "FR", "fr", ["FR"]),
    ("superhairpieces.es", "ES", "es", ["ES"]),
    ("superhairpieces.de", "DE", "de", ["DE"]),
]


def display_name_for(storefront):
    return f"{storefront} API feed"


def add_account_argument(parser: argparse.ArgumentParser):
    parser.add_argument(
        "--account",
        help="Merchant Center account id (the number in the top-right of Merchant "
        "Center). Defaults to the GMC_ACCOUNT_ID environment variable.",
    )


def resolve_account(args) -> str:
    load_dotenv()
    account = args.account or os.environ.get("GMC_ACCOUNT_ID")
    if not account:
        sys.exit("error: pass --account or set GMC_ACCOUNT_ID")
    account = str(account).strip()
    if not account.isdigit():
        sys.exit(f"error: account id must be numeric, got {account!r}")
    return account


def account_parent(account: str) -> str:
    return f"accounts/{account}"


def caller_identity() -> str:
    """Best-effort description of the ADC identity, for error messages."""
    try:
        import google.auth

        creds, _ = google.auth.default()
        return getattr(creds, "service_account_email", None) or "your logged-in Google account"
    except Exception:  # noqa: BLE001 - purely informational
        return "the current Application Default Credentials identity"


def explain_api_error(exc) -> str:
    """Turn the Merchant API's common setup errors into a next step."""
    text = str(exc)
    if "GCP_NOT_REGISTERED" in text:
        return (
            f"GCP project {GCP_PROJECT} is not registered with this Merchant Center account. "
            "Run: python google-merchant/register_developer.py --account <id> --email <you> "
            "(then allow ~5 minutes)."
        )
    if "PERMISSION_DENIED" in text or "403" in text:
        return (
            f"{caller_identity()} is not a user on this Merchant Center account. In Merchant "
            "Center go to Settings -> People and access, add that email with Admin access, "
            "wait a few minutes, then retry."
        )
    return text
