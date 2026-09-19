"""One-time browser sign-in to Google Chat as yourself; stores the token in Secret Manager.

Run this on your own machine (it opens a browser). It never prints the token.

    python google-chat/oauth_login.py --client-secrets %USERPROFILE%\\Downloads\\client_secret.json

What it does:
  1. Opens the Google consent screen for the read-only Chat scopes in chat_api.SCOPES.
  2. Confirms the token works by counting your spaces.
  3. Stores the resulting authorized_user JSON as Secret Manager secret
     `google-chat-user-token` (creating it if needed) via your local gcloud.
  4. Grants each --grant service account read access to that secret only,
     so Claude cloud sessions can use it.

Prerequisites (Cloud console, project shp-ai-bot-2026, each once):
  * Google Chat API enabled AND configured (APIs & Services -> Google Chat API ->
    Configuration: app name, avatar URL, description). Chat rejects user-auth
    calls from a project without an app configuration.
  * OAuth consent screen with Audience = Internal.
  * An OAuth 2.0 client of type "Desktop app"; download its JSON. That file
    is a credential - keep it out of the repo.
  * `pip install -r google-chat/requirements.txt` and `gcloud auth login`.

Re-running replaces the token with a new version; scripts read `latest`.
"""

import argparse
import shutil
import subprocess
import sys

from chat_api import GCP_PROJECT, SCOPES, TOKEN_SECRET_ID, ChatClient, ChatApiError

DEFAULT_GRANTS = ["claude-sessions@shp-ai-bot-2026.iam.gserviceaccount.com"]


def gcloud(*args, stdin=None, check=True):
    exe = shutil.which("gcloud")
    if not exe:
        sys.exit("error: gcloud not found on PATH; install the Cloud SDK or run `gcloud auth login` first")
    return subprocess.run([exe, *args], input=stdin, text=True, capture_output=True, check=check)


def store_secret(secret_id, project, payload):
    exists = gcloud("secrets", "describe", secret_id, "--project", project, check=False).returncode == 0
    if not exists:
        gcloud("secrets", "create", secret_id, "--replication-policy=automatic", "--project", project)
        print(f"created secret {secret_id}")
    gcloud("secrets", "versions", "add", secret_id, "--data-file=-", "--project", project, stdin=payload)
    print(f"stored token as a new version of {secret_id}")


def grant_reader(secret_id, project, service_account):
    gcloud(
        "secrets", "add-iam-policy-binding", secret_id,
        "--member", f"serviceAccount:{service_account}",
        "--role", "roles/secretmanager.secretAccessor",
        "--project", project,
    )
    print(f"granted secretAccessor on {secret_id} to {service_account}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-secrets", required=True, help="Path to the Desktop-app OAuth client JSON downloaded from Cloud console")
    parser.add_argument("--secret", default=TOKEN_SECRET_ID, help=f"Secret Manager secret to write (default {TOKEN_SECRET_ID})")
    parser.add_argument("--project", default=GCP_PROJECT)
    parser.add_argument("--grant", action="append", metavar="SERVICE_ACCOUNT",
                        help=f"Service account to grant read access (repeatable; default {DEFAULT_GRANTS[0]})")
    parser.add_argument("--no-grant", action="store_true", help="Store the token without granting any service account")
    parser.add_argument("--no-browser", action="store_true", help="Print the auth URL instead of opening a browser (headless machines)")
    args = parser.parse_args(argv)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("error: pip install -r google-chat/requirements.txt")

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=not args.no_browser, prompt="consent")

    # Prove the token works before persisting it.
    try:
        spaces = ChatClient(creds).list_spaces()
    except ChatApiError as exc:
        sys.exit(f"error: signed in, but the Chat API rejected the token: {exc}\n"
                 "If it says the Chat app isn't configured, fill in APIs & Services -> Google Chat API -> Configuration.")
    print(f"signed in; you are a member of {len(spaces)} spaces (DMs/group chats count once a message exists)")

    store_secret(args.secret, args.project, creds.to_json())
    if not args.no_grant:
        for sa in args.grant or DEFAULT_GRANTS:
            grant_reader(args.secret, args.project, sa)
    print("done. Next: python google-chat/list_spaces.py")


if __name__ == "__main__":
    main()
