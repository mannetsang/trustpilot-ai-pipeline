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

from merchant_api import (
    GCP_PROJECT,
    GCP_PROJECT_NUMBER,
    account_parent,
    add_account_argument,
    explain_api_error,
    resolve_account,
)


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
    # The API reports project *numbers* (e.g. 304363458561), not project ids, and it
    # lists every project registered against the account - an agency's or an app's as
    # readily as ours. A non-empty list is not the same as "we are in it": account
    # 289630622 is registered to project 40060666568 and not to us.
    #
    # Which matters less than it looks. A project registers against exactly one
    # Merchant Center account, and that one registration then covers every other
    # account the caller can reach:
    #
    #   "A single Google Cloud project can only be registered to exactly one
    #    Merchant Center account at a time [...] you can use that same project to
    #    manage any other Merchant Center account you have access to."
    #   - developers.google.com/merchant/api/guides/quickstart/faq
    #
    # So this is a report on where our one registration lives, not a gate. Verified
    # on 2026-10-21: writes to 289630622 and 670525760, neither registered to us,
    # reach field validation rather than GCP_NOT_REGISTERED. Registering a second
    # account returns 409 DUPLICATE_PROJECT_REGISTRATION and is not what you want.
    registered = list(reg.gcp_ids)
    print(f"registered GCP project numbers: {registered or '(none)'}")
    if GCP_PROJECT_NUMBER in registered:
        print(f"ok: {GCP_PROJECT} ({GCP_PROJECT_NUMBER}) is registered with this account "
              "(allow ~5 minutes before other calls)")
        return 0
    print(f"note: this account does not hold our registration"
          + (f" (it is registered to {', '.join(registered)})" if registered else " (it holds none)")
          + f". That is fine as long as {GCP_PROJECT} ({GCP_PROJECT_NUMBER}) is registered "
          "against one account we can reach - the registration is per project, not per "
          "account, and covers the rest. Check the account that holds it, and only "
          "register if no account does; registering a second returns 409.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
