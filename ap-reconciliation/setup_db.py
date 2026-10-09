"""Apply schema.sql to the AP reconciliation Supabase project.

The service has its own Supabase project (nothing else lives in it), and the
schema is plain SQL that is safe to re-run: every statement is `create ... if
not exists` or `on conflict do nothing`. This script sends it through the
Supabase Management API so the setup is one command instead of a paste into
the SQL editor, and so a later schema change is applied the same way:

    python ap-reconciliation/setup_db.py --project-ref <ref>
    python ap-reconciliation/setup_db.py --project-ref <ref> --dry-run   # print the SQL only

The project ref is the 20-character id in the project's URL
(https://<ref>.supabase.co; Project Settings > General). The Management API
token is a Supabase personal access token (Account > Access Tokens), resolved
through the repo's lib/secrets.py: the SUPABASE_ACCESS_TOKEN environment
variable (or the gitignored .env) first, then Secret Manager
`SUPABASE_ACCESS_TOKEN` on shp-ai-bot-2026. It is a different credential from
the project's service-role key, which the running service uses.

Standard library plus requests; no Supabase client needed.
"""

import argparse
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from lib.secrets import SecretNotFound, get_secret  # noqa: E402

MANAGEMENT_API = "https://api.supabase.com/v1"
DEFAULT_SCHEMA = os.path.join(HERE, "schema.sql")


def apply_schema(project_ref, sql, token, timeout=120):
    """Run `sql` on the project's database. Returns (status code, response text)."""
    url = f"{MANAGEMENT_API}/projects/{project_ref}/database/query"
    response = requests.post(
        url,
        json={"query": sql},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    return response.status_code, response.text


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply schema.sql to a Supabase project.")
    parser.add_argument("--project-ref", required=True, help="the project's ref, from https://<ref>.supabase.co")
    parser.add_argument("--schema", default=DEFAULT_SCHEMA, help="SQL file to apply (default: schema.sql next to this script)")
    parser.add_argument("--dry-run", action="store_true", help="print the SQL and exit without calling Supabase")
    args = parser.parse_args(argv)

    with open(args.schema, encoding="utf-8") as handle:
        sql = handle.read()
    statements = sum(1 for line in sql.splitlines() if line.rstrip().endswith(";"))

    if args.dry_run:
        print(sql)
        print(f"dry run: {statements} statements from {args.schema} would be applied to project {args.project_ref}")
        return 0

    try:
        token = get_secret("SUPABASE_ACCESS_TOKEN", env_var="SUPABASE_ACCESS_TOKEN")
    except SecretNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    status, text = apply_schema(args.project_ref, sql, token)
    if 200 <= status < 300:
        print(f"ok: applied {statements} statements from {os.path.basename(args.schema)} to project {args.project_ref}")
        return 0
    # The API answers with JSON like {"message": "..."} on a failed statement;
    # the first few hundred characters hold the Postgres error.
    print(f"error: Supabase Management API returned {status}: {text[:400]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
