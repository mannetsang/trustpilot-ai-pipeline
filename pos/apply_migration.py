"""Apply the POS schema migrations to the Supabase Postgres database.

Runs every file in pos/supabase/migrations/ in name order, skipping the ones
already recorded in pos_schema_migrations. Each file runs in its own
transaction, so a failing migration leaves the database as it was.

Two ways in, both resolved through lib/secrets.py (an environment variable or
gitignored .env first, then GCP Secret Manager on project shp-ai-bot-2026):

  api  The Supabase Management API over HTTPS, authenticated with the
       SUPABASE_ACCESS_TOKEN secret (a personal access token). Works from
       anywhere with HTTPS, including Claude cloud sessions. Preferred.
  db   A direct Postgres connection with the SUPABASE_DB_URL secret, through
       the psycopg package or the psql command. Needs outbound port 5432 or
       6543, which cloud sessions do not have.

By default the API is used when SUPABASE_ACCESS_TOKEN resolves, else the
database. Force one with --via api or --via db.

Usage (one line, works in Windows cmd):

    python pos/apply_migration.py            apply what is pending
    python pos/apply_migration.py --list     show applied and pending, change nothing
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.secrets import SecretNotFound, get_secret  # noqa: E402

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase", "migrations")

# The Supabase project reference is the subdomain of the project's API URL. It
# is an identifier, not a secret, and doubles as the default for the API path.
SUPABASE_PROJECT_REF = os.environ.get("SUPABASE_PROJECT_REF", "ngwlwntvoteuafeplobx")
SUPABASE_API = "https://api.supabase.com/v1"
USER_AGENT = "trustpilot-ai-pipeline pos/apply_migration"
BOOTSTRAP_SQL = (
    "create table if not exists pos_schema_migrations ("
    "name text primary key, applied_at timestamptz not null default now());"
)


def migration_files():
    names = sorted(n for n in os.listdir(MIGRATIONS_DIR) if n.endswith(".sql"))
    return [(n, os.path.join(MIGRATIONS_DIR, n)) for n in names]


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Three interchangeable backends. Each exposes applied() and apply(name, sql).
# ---------------------------------------------------------------------------


class ApiBackend:
    """Runs SQL through the Supabase Management API's database/query endpoint."""

    def __init__(self, token, project_ref):
        self.url = f"{SUPABASE_API}/projects/{project_ref}/database/query"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Cloudflare in front of the API rejects Python's default agent string.
            "User-Agent": USER_AGENT,
        }
        self._run(BOOTSTRAP_SQL)

    def _run(self, sql):
        body = json.dumps({"query": sql}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            raise RuntimeError(f"Supabase API returned {exc.code}: {detail}") from None
        return json.loads(raw) if raw else []

    def applied(self):
        rows = self._run("select name from pos_schema_migrations order by name;")
        return [row["name"] for row in rows]

    def apply(self, name, sql):
        safe_name = name.replace("'", "''")
        script = f"begin;\n{sql}\ninsert into pos_schema_migrations (name) values ('{safe_name}');\ncommit;\n"
        self._run(script)

    def close(self):
        pass


class PsycopgBackend:
    def __init__(self, url):
        import psycopg  # noqa: WPS433 - optional dependency

        self.conn = psycopg.connect(url, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute(BOOTSTRAP_SQL)
        self.conn.commit()

    def applied(self):
        with self.conn.cursor() as cur:
            cur.execute("select name from pos_schema_migrations order by name")
            return [row[0] for row in cur.fetchall()]

    def apply(self, name, sql):
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("insert into pos_schema_migrations (name) values (%s)", (name,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self):
        self.conn.close()


class PsqlBackend:
    """Drives the psql command. The password travels in PGPASSWORD, never argv."""

    def __init__(self, url):
        parts = urlparse(url)
        if parts.scheme not in ("postgres", "postgresql"):
            raise SystemExit(f"SUPABASE_DB_URL must be a postgresql:// URL, got scheme '{parts.scheme}'")
        self.env = dict(os.environ)
        if parts.password:
            self.env["PGPASSWORD"] = parts.password
        self.args = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1"]
        if parts.hostname:
            self.args += ["-h", parts.hostname]
        if parts.port:
            self.args += ["-p", str(parts.port)]
        if parts.username:
            self.args += ["-U", parts.username]
        dbname = parts.path.lstrip("/") or "postgres"
        self.args += ["-d", dbname]
        for pair in parts.query.split("&") if parts.query else []:
            key, _, value = pair.partition("=")
            if key == "sslmode" and value:
                self.env["PGSSLMODE"] = value
        self._run(BOOTSTRAP_SQL)

    def _run(self, sql, capture=False):
        result = subprocess.run(
            self.args + (["-At"] if capture else []),
            input=sql,
            env=self.env,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"psql exited {result.returncode}")
        return result.stdout

    def applied(self):
        out = self._run("select name from pos_schema_migrations order by name;", capture=True)
        return [line for line in out.splitlines() if line]

    def apply(self, name, sql):
        safe_name = name.replace("'", "''")
        script = f"begin;\n{sql}\ninsert into pos_schema_migrations (name) values ('{safe_name}');\ncommit;\n"
        self._run(script)

    def close(self):
        pass


def connect_db():
    try:
        url = get_secret("SUPABASE_DB_URL", env_var="SUPABASE_DB_URL")
    except SecretNotFound as exc:
        raise SystemExit(str(exc))
    try:
        import psycopg  # noqa: F401
    except ImportError:
        if not shutil.which("psql"):
            raise SystemExit(
                'Neither the psycopg package nor the psql command is available. '
                'Run: pip install "psycopg[binary]"'
            )
        print("via psql")
        return PsqlBackend(url)
    print("via psycopg")
    return PsycopgBackend(url)


def connect_api(token=None):
    if token is None:
        try:
            token = get_secret("SUPABASE_ACCESS_TOKEN", env_var="SUPABASE_ACCESS_TOKEN")
        except SecretNotFound as exc:
            raise SystemExit(str(exc))
    print(f"via Supabase API, project {SUPABASE_PROJECT_REF}")
    return ApiBackend(token, SUPABASE_PROJECT_REF)


def connect(via):
    if via == "db":
        return connect_db()
    if via == "api":
        return connect_api()
    token = get_secret("SUPABASE_ACCESS_TOKEN", env_var="SUPABASE_ACCESS_TOKEN", required=False)
    return connect_api(token) if token else connect_db()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="show applied and pending migrations, change nothing")
    parser.add_argument("--via", choices=["auto", "api", "db"], default="auto",
                        help="api = Supabase Management API over HTTPS, db = direct Postgres (default: api if its token resolves, else db)")
    args = parser.parse_args()

    try:
        backend = connect(args.via)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    try:
        done = set(backend.applied())
        pending = [(n, p) for n, p in migration_files() if n not in done]

        for name in sorted(done):
            print(f"applied  {name}")
        for name, _ in pending:
            print(f"pending  {name}")
        if args.list or not pending:
            if not pending:
                print("nothing to do")
            return

        for name, path in pending:
            print(f"applying {name} ...", end=" ", flush=True)
            try:
                backend.apply(name, read(path))
            except RuntimeError as exc:
                print("FAILED")
                raise SystemExit(str(exc))
            print("ok")
    finally:
        backend.close()


if __name__ == "__main__":
    main()
