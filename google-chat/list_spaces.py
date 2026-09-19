"""List every Google Chat space, group chat and DM you are in, or read one of them.

    python google-chat/list_spaces.py                      # all spaces, most recently active first
    python google-chat/list_spaces.py --type DIRECT_MESSAGE
    python google-chat/list_spaces.py --messages spaces/AAAAxxxxxxx --limit 30
    python google-chat/list_spaces.py --json               # raw API objects

Needs the user token from oauth_login.py (Secret Manager `google-chat-user-token`,
or GOOGLE_CHAT_USER_TOKEN in a local .env).
"""

import argparse
import json
import sys

from chat_api import ChatApiError, ChatClient, describe_space
from lib.secrets import SecretNotFound

SPACE_TYPES = ["SPACE", "GROUP_CHAT", "DIRECT_MESSAGE"]


def print_spaces(client, spaces):
    spaces.sort(key=lambda s: s.get("lastActiveTime", ""), reverse=True)
    print(f"{'last active':<20} {'type':<15} {'members':>7}  {'name':<22} label")
    for s in spaces:
        last = (s.get("lastActiveTime") or "")[:19].replace("T", " ")
        print(f"{last:<20} {s.get('spaceType', ''):<15} {s.get('membershipCount', {}).get('joinedDirectHumanUserCount', ''):>7}  "
              f"{s['name']:<22} {describe_space(client, s)}")
    print(f"\n{len(spaces)} spaces")


def print_messages(msgs):
    for m in reversed(msgs):  # oldest of the batch first, reads like a transcript
        when = (m.get("createTime") or "")[:19].replace("T", " ")
        sender = (m.get("sender") or {})
        who = sender.get("displayName") or sender.get("name", "?")
        text = (m.get("text") or m.get("formattedText") or "").replace("\n", "\n" + " " * 41)
        attachments = f" [{len(m['attachment'])} attachment(s)]" if m.get("attachment") else ""
        print(f"{when}  {who:<18} {text}{attachments}")
    print(f"\n{len(msgs)} messages")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--type", choices=SPACE_TYPES, help="Only this space type")
    parser.add_argument("--messages", metavar="SPACE", help="Read messages from this space (spaces/XXXX)")
    parser.add_argument("--limit", type=int, default=50, help="Messages to fetch with --messages (default 50)")
    parser.add_argument("--json", action="store_true", help="Print raw API JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        client = ChatClient()
        if args.messages:
            msgs = client.list_messages(args.messages, limit=args.limit)
            if args.json:
                print(json.dumps(msgs, indent=2, ensure_ascii=False))
            else:
                print_messages(msgs)
        else:
            spaces = client.list_spaces(args.type)
            if args.json:
                print(json.dumps(spaces, indent=2, ensure_ascii=False))
            else:
                print_spaces(client, spaces)
    except SecretNotFound as exc:
        sys.exit(f"error: no Chat user token yet ({exc}). Run google-chat/oauth_login.py on your machine first.")
    except ChatApiError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
