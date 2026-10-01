"""List every Business Profile location the token can manage.

    python google-business/locations.py          # table
    python google-business/locations.py --json   # raw API objects

Account and location ids are not secrets; the ids printed here are what the
other scripts take as --location arguments.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gbp_api import GbpClient, format_address  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="List Business Profile locations")
    parser.add_argument("--json", action="store_true", help="print the raw API objects")
    args = parser.parse_args()

    client = GbpClient()
    accounts = client.list_accounts()
    if args.json:
        out = [{"account": a, "locations": client.list_locations(a["name"])} for a in accounts]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    total = 0
    for account in accounts:
        print(f"{account.get('accountName')}  ({account['name']}, {account.get('type')}, "
              f"verification={account.get('verificationState')})")
        locations = client.list_locations(account["name"])
        total += len(locations)
        for loc in sorted(locations, key=lambda l: (l.get("title", ""), format_address(l))):
            status = (loc.get("openInfo") or {}).get("status", "?")
            primary = ((loc.get("categories") or {}).get("primaryCategory") or {}).get("displayName", "")
            print(f"  {loc['name']:<30} {status:<20} {loc.get('title')}")
            print(f"  {'':<30} {'':<20} {format_address(loc)}")
            if primary:
                print(f"  {'':<30} {'':<20} {primary}")
    print(f"\n{len(accounts)} account(s), {total} location(s)")


if __name__ == "__main__":
    main()
