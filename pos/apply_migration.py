"""Apply the POS schema migrations to the Supabase Postgres database.

Runs every file in pos/supabase/migrations/ in name order, skipping the ones
already recorded in pos_schema_migrations. Each file runs in its own
transaction, so a failing migration leaves the database as it was.

The connection string is the SUPABASE_DB_URL secret, resolved through
lib/secrets.py: the SUPABASE_DB_URL environment variable (or a gitignored
.env) first, then GCP Secret Manager on project shp-ai-bot-2026. Direct
Postgres connections need port 5432 (or 6543 for the pooler) open outbound;
if you only have HTTPS, paste the .sql into the Supabase SQL editor instead.

Usage (one line, works in Windows cmd):

    python pos/apply_migration.py            apply what is pending
    python pos/apply_migration.py --list     show applied and pending, change nothing

Needs either the psycopg package (pip install "psycopg[binary]") or the psql
command on PATH. psycopg is tried first.
"""

import argparse
import os
import shutil
import subprocess
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.secrets import SecretNotFound, get_secret  # noqa: E402

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase", "migrations")
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
# Two interchangeable backends. Each exposes applied() and apply(name, sql).
# ---------------------------------------------------------------------------


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


def connect(url):
    try:
        import psycopg  # noqa: F401
    except ImportError:
        if not shutil.which("psql"):
            raise SystemExit(
                'Neither the psycopg package nor the psql command is available. '
                'Run: pip install "psycopg[binary]"'
            )
        return PsqlBackend(url)
    return PsycopgBackend(url)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="show applied and pending migrations, change nothing")
    args = parser.parse_args()

    try:
        url = get_secret("SUPABASE_DB_URL", env_var="SUPABASE_DB_URL")
    except SecretNotFound as exc:
        raise SystemExit(str(exc))

    backend = connect(url)
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
            backend.apply(name, read(path))
            print("ok")
    finally:
        backend.close()


if __name__ == "__main__":
    main()
