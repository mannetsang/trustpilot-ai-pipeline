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

# A key defined more than once keeps its plain name for the LAST definition,
# matching python-dotenv (the loader bc-seo-audit uses). Earlier definitions
# are kept under a suffixed name rather than silently dropped.


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
        # last occurrence wins the plain name; earlier ones get a suffix
        name = k if i == last_index[k] else f"{k}__PREV{i}"
        if name in seen:
            continue
        seen.add(name)
        out.append((name, v))
    return out


def gcloud(args, project, stdin=None):
    cmd = [GCLOUD] + args + ["--project", project]
    return subprocess.run(cmd, input=stdin, capture_output=True)


def active_account():
    """The account gcloud will run as, or None when signed out."""
    r = subprocess.run([GCLOUD, "auth", "list", "--filter=status:ACTIVE",
                        "--format=value(account)"], capture_output=True)
    if r.returncode != 0:
        return None
    return r.stdout.decode(errors="replace").strip() or None


def error_line(r):
    """The most informative line of a failed gcloud call's stderr."""
    lines = [l for l in r.stderr.decode(errors="replace").splitlines() if l.strip()]
    for line in lines:
        if line.startswith("ERROR"):
            return line.strip()
    return lines[-1].strip() if lines else "no error output"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="shp-ai-bot-2026")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = parse_env(args.env)
    print(f"{len(entries)} secrets to sync from {args.env} -> project {args.project}")

    if not args.dry_run:
        account = active_account()
        if not account:
            print("\nNo active gcloud account, so every secret would fail. Sign in with:")
            print("    gcloud auth login")
            print(f"    gcloud config set project {args.project}")
            return 1
        print(f"signed in as {account}")
    print()

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
                print(f"FAILED   {name}: {error_line(r)}")
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
            print(f"FAILED   {name}: {error_line(r)}")

    print(f"\ncreated={created} new_versions={updated} unchanged={unchanged} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
