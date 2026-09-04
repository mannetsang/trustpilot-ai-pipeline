"""Register the GCP project with a Merchant Center account (one-time).

Every Merchant API call is rejected with GCP_NOT_REGISTERED until the calling
project has been registered against the account once. This is that call.

    python google-merchant/register_developer.py --account 123456789 --email manne@superhairpieces.com

The caller must already be a user on the account (Settings -> People and
access). Re-running is harmless: the API returns the existing registration.
"""

import argparse
import sys

from google.api_core import exceptions as gexc
from google.shopping.merchant_accounts_v1 import (
    DeveloperRegistrationServiceClient,
    GetDeveloperRegistrationRequest,
    RegisterGcpRequest,
)

from merchant_api import GCP_PROJECT, account_parent, add_account_argument, explain_api_error, resolve_account


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_account_argument(parser)
    parser.add_argument("--email", required=True, help="Contact email Google keeps for this developer registration")
    parser.add_argument("--check", action="store_true", help="Only read the current registration; do not register")
    args = parser.parse_args(argv)

    account = resolve_account(args)
    name = f"{account_parent(account)}/developerRegistration"
    client = DeveloperRegistrationServiceClient()

    try:
        if args.check:
            reg = client.get_developer_registration(request=GetDeveloperRegistrationRequest(name=name))
        else:
            reg = client.register_gcp(request=RegisterGcpRequest(name=name, developer_email=args.email))
    except gexc.GoogleAPICallError as exc:
        sys.exit(f"error: {explain_api_error(exc)}")

    print(f"registration: {reg.name}")
    print(f"registered GCP ids: {list(reg.gcp_ids) or '(none)'}")
    if GCP_PROJECT not in reg.gcp_ids and not any(GCP_PROJECT in g for g in reg.gcp_ids):
        print(f"warning: {GCP_PROJECT} is not in the registered list yet; allow ~5 minutes and re-run with --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
