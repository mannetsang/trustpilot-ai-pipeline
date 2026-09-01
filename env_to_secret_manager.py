#!/usr/bin/env python3
"""Copy every credential in the root .env into GCP Secret Manager.

Idempotent: creates a secret if it does not exist, adds a new version only when
the value differs from the current latest version, and skips otherwise.
Values are piped over stdin (--data-file=-) so they never appear in argv.

Usage:
    python env_to_secret_manager.py [--dry-run] [--project PROJECT] [--env PATH]
"""
import argparse
import shutil
import subprocess
import sys

# On Windows the CLI is gcloud.cmd; subprocess without shell=True needs the
# resolved path, so look it up once via PATHEXT-aware which().
GCLOUD = shutil.which("gcloud") or "gcloud"

# The .env defines BIGCOMMERCE_gmosz3ja_* twice with different values: an older
# "AI Agent Test" app and a newer "Man POS machine Aug 30 2026" app. The newer
# (last) one keeps the plain secret name; the older set is preserved under a
# disambiguated name instead of being silently dropped.
RENAME_EARLIER = {
    "BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN": "BIGCOMMERCE_gmosz3ja_AI_AGENT_TEST_ACCESS_TOKEN",
    "BIGCOMMERCE_gmosz3ja_CLIENT_NAME": "BIGCOMMERCE_gmosz3ja_AI_AGENT_TEST_CLIENT_NAME",
    "BIGCOMMERCE_gmosz3ja_CLIENT_ID": "BIGCOMMERCE_gmosz3ja_AI_AGENT_TEST_CLIENT_ID",
    "BIGCOMMERCE_gmosz3ja_CLIENT_SECRET": "BIGCOMMERCE_gmosz3ja_AI_AGENT_TEST_CLIENT_SECRET",
    "BIGCOMMERCE_gmosz3ja_API_NAME": "BIGCOMMERCE_gmosz3ja_AI_AGENT_TEST_API_NAME",
    "BIGCOMMERCE_gmosz3ja_API_PATH": "BIGCOMMERCE_gmosz3ja_AI_AGENT_TEST_API_PATH",
}


def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse_env(path):
    """Return an ordered list of (secret_name, value), duplicates disambiguated."""
    raw = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            raw.append((k.strip(), unquote(v)))

    last_index = {}
    for i, (k, _) in enumerate(raw):
        last_index[k] = i

    out, seen = [], set()
    for i, (k, v) in enumerate(raw):
        if i == last_index[k]:
            name = k                       # last occurrence wins the plain name
        elif k in RENAME_EARLIER:
            name = RENAME_EARLIER[k]
        else:
            name = f"{k}__PREV{i}"
        if name in seen:
            continue
        seen.add(name)
        out.append((name, v))
    return out


def gcloud(args, project, stdin=None):
    cmd = [GCLOUD] + args + ["--project", project]
    return subprocess.run(cmd, input=stdin, capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="shp-ai-bot-2026")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = parse_env(args.env)
    print(f"{len(entries)} secrets to sync from {args.env} -> project {args.project}\n")

    created = updated = unchanged = failed = 0
    for name, value in entries:
        if args.dry_run:
            print(f"[dry-run] {name} ({len(value)} chars)")
            continue

        data = value.encode("utf-8")
        exists = gcloud(["secrets", "describe", name], args.project).returncode == 0

        if not exists:
            r = gcloud(["secrets", "create", name, "--replication-policy=automatic",
                        "--labels=source=root-dotenv", "--data-file=-"],
                       args.project, stdin=data)
            if r.returncode == 0:
                created += 1
                print(f"created  {name}")
            else:
                failed += 1
                print(f"FAILED   {name}: {r.stderr.decode(errors='replace').strip().splitlines()[-1]}")
            continue

        cur = gcloud(["secrets", "versions", "access", "latest", "--secret", name], args.project)
        if cur.returncode == 0 and cur.stdout == data:
            unchanged += 1
            print(f"same     {name}")
            continue

        r = gcloud(["secrets", "versions", "add", name, "--data-file=-"], args.project, stdin=data)
        if r.returncode == 0:
            updated += 1
            print(f"new ver  {name}")
        else:
            failed += 1
            print(f"FAILED   {name}: {r.stderr.decode(errors='replace').strip().splitlines()[-1]}")

    print(f"\ncreated={created} new_versions={updated} unchanged={unchanged} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
