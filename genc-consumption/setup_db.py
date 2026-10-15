"""Apply schema.sql to the GenC Consumption Supabase project.

    python genc-consumption/setup_db.py
    python genc-consumption/setup_db.py --dry-run          # print the SQL only

The SQL is safe to re-run (`if not exists` throughout). It goes through the
Supabase Management API, authenticated with the personal access token in
Secret Manager `SUPABASE_ACCESS_TOKEN` on shp-ai-bot-2026 (or the
SUPABASE_ACCESS_TOKEN entry of the gitignored root .env), resolved by
lib/secrets.py. That token is not the service-role key the dashboard uses.
"""

import argparse
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from lib.secrets import SecretNotFound, get_secret  # noqa: E402

PROJECT_REF = "zitrqrchbdracjvxbwtn"
MANAGEMENT_API = "https://api.supabase.com/v1"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply schema.sql to the GenC Consumption Supabase project.")
    parser.add_argument("--project-ref", default=PROJECT_REF)
    parser.add_argument("--schema", default=os.path.join(HERE, "schema.sql"))
    parser.add_argument("--dry-run", action="store_true", help="print the SQL and exit")
    args = parser.parse_args(argv)

    with open(args.schema, encoding="utf-8") as handle:
        sql = handle.read()
    if args.dry_run:
        print(sql)
        return 0

    try:
        token = get_secret("SUPABASE_ACCESS_TOKEN", env_var="SUPABASE_ACCESS_TOKEN", project="shp-ai-bot-2026")
    except SecretNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    response = requests.post(
        f"{MANAGEMENT_API}/projects/{args.project_ref}/database/query",
        json={"query": sql},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=120,
    )
    if response.ok:
        print(f"ok: applied {os.path.basename(args.schema)} to project {args.project_ref}")
        return 0
    print(f"error: Supabase Management API returned {response.status_code}: {response.text[:400]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
