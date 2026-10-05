"""Google Chat from a Claude Code session, as Manne, with the same sign-in Man AI uses.

The sign-in (Secret Manager: chat-assistant-user-token) is read inside this process and never printed.
Nothing is sent without --confirm: run without it first to show the exact message, and add --confirm
only after Manne has approved that text.

  python chat-assistant/scripts/gchat.py list [--query TEXT]
  python chat-assistant/scripts/gchat.py send --to sydney@superhairpieces.com --text "..." [--confirm]
  python chat-assistant/scripts/gchat.py send --space "Space name or spaces/ID" --text "..." [--confirm]

--to works when Manne already has a direct message with that person. Starting a new one needs the
chat.spaces.create permission, which the sign-in doesn't have.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [ROOT, os.path.join(ROOT, "chat-assistant", "src", "cloud_run")]

from google_apis import CHAT, GoogleApiError, GoogleClient, credentials_from_json  # noqa: E402

TOKEN_SECRET = os.environ.get("USER_TOKEN_SECRET_ID", "chat-assistant-user-token")
MAX_TEXT = 4000
KINDS = {"DIRECT_MESSAGE": "direct message", "GROUP_CHAT": "group chat", "SPACE": "space"}


class Stop(Exception):
    """A problem to report in one line (no traceback)."""


def make_client():
    from lib.secrets import get_secret

    return GoogleClient(credentials_from_json(get_secret(TOKEN_SECRET)))


def find_space(google, to=None, space=None):
    """The conversation to post in: the direct message with `to` (an email), or a space by id or name."""
    if to:
        try:
            return google.request("GET", f"{CHAT}/spaces:findDirectMessage", params={"name": f"users/{to}"})
        except GoogleApiError as exc:
            if exc.status == 404:
                raise Stop(f"There's no direct message with {to} yet, and starting one needs a Google "
                           "permission the sign-in doesn't have. Use a space they're in (--space).") from exc
            raise
    if space.startswith("spaces/"):
        return google.request("GET", f"{CHAT}/{space}")
    wanted = space.strip().lower()
    named = [s for s in google.list_spaces() if s.get("displayName")]
    exact = [s for s in named if s["displayName"].lower() == wanted]
    matches = exact or [s for s in named if wanted in s["displayName"].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise Stop(f"No space is named like '{space}'. Run `list` to see them.")
    raise Stop(f"'{space}' matches {len(matches)} spaces; use its spaces/ID:\n"
               + "\n".join(f"  {s['name']}  {s['displayName']}" for s in matches[:20]))


def cmd_list(google, query="", out=print):
    spaces = sorted(google.list_spaces(), key=lambda s: s.get("lastActiveTime", ""), reverse=True)
    shown = 0
    for s in spaces:
        name = s.get("displayName") or ""
        if query and query.lower() not in name.lower():
            continue
        out(f"{s['name']}\t{KINDS.get(s.get('spaceType'), s.get('spaceType', ''))}\t"
            f"{name or '(no name: reach it with --to EMAIL)'}\t{(s.get('lastActiveTime') or '')[:10]}")
        shown += 1
    out(f"{shown} conversation(s).")


def cmd_send(google, text, to=None, space=None, confirm=False, out=print):
    text = (text or "").strip()
    if not text:
        raise Stop("The message is empty.")
    if len(text) > MAX_TEXT:
        raise Stop(f"The message is {len(text)} characters; Google Chat takes up to {MAX_TEXT}.")
    target = find_space(google, to=to, space=space)
    label = target.get("displayName") or to or target["name"]
    out(f"To: {label} ({target['name']})\nFrom: Manne\n---\n{text}\n---")
    if not confirm:
        out("Not sent. Run again with --confirm once Manne has approved this exact text.")
        return None
    sent = google.reply_in_thread(target["name"], None, text)
    out(f"Sent ({sent.get('name', '')}, {sent.get('createTime', '')}).")
    return sent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Google Chat as Manne (nothing is sent without --confirm).")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="Conversations, most recently active first")
    listing.add_argument("--query", default="", help="Only names containing this")
    send = sub.add_parser("send", help="Show a message, and send it with --confirm")
    who = send.add_mutually_exclusive_group(required=True)
    who.add_argument("--to", help="Email of the person (an existing direct message)")
    who.add_argument("--space", help="Space name, or spaces/ID from `list`")
    send.add_argument("--text", required=True)
    send.add_argument("--confirm", action="store_true", help="Really send (only after Manne approved the text)")
    args = parser.parse_args(argv)

    try:
        google = make_client()
        if args.command == "list":
            cmd_list(google, args.query)
        else:
            cmd_send(google, args.text, to=args.to, space=args.space, confirm=args.confirm)
    except Stop as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - one line, no traceback (and never the sign-in itself)
        print(f"{type(exc).__name__}: {str(exc)[:500]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
