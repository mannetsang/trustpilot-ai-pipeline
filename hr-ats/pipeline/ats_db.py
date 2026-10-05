"""Supabase access for the HR ATS jobs (indeed-pipeline, offer-packet).

Talks to the `shp-ats` project's REST (PostgREST) and Storage APIs with the
server-side secret key, using plain `requests` so the image needs nothing new.

Credentials:
  SUPABASE_URL          not secret; defaults to the shp-ats project.
  SUPABASE_SECRET_KEY   env var (Cloud Run injects it from Secret Manager
                        `shp-ats-supabase-secret-key`); falls back to
                        Secret Manager directly via lib/secrets.py-style ADC
                        lookup when run locally without it.
"""
import os
import re
import socket
import uuid
from urllib.parse import quote

import requests

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://qxmwygkwctyksfcmsqsf.supabase.co").rstrip("/")
SECRET_NAME = "shp-ats-supabase-secret-key"
GCP_PROJECT = "shp-ai-bot-2026"
RESUME_BUCKET = "resumes"

_key = None


def _secret_key():
    global _key
    if _key:
        return _key
    _key = os.environ.get("SUPABASE_SECRET_KEY")
    if not _key:
        from google.cloud import secretmanager  # local runs only
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{GCP_PROJECT}/secrets/{SECRET_NAME}/versions/latest"
        _key = client.access_secret_version(name=name).payload.data.decode().strip()
    return _key


def _headers(extra=None):
    key = _secret_key()
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _check(r, what):
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase {what} failed {r.status_code}: {r.text[:500]}")
    return r


# ---------------------------------------------------------------- REST
def select(table, params=None, page_size=1000):
    """GET every matching row, following PostgREST's 1000-row pages."""
    rows, start = [], 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}", params=params or {},
            headers=_headers({"Range-Unit": "items",
                              "Range": f"{start}-{start + page_size - 1}"}),
            timeout=30)
        _check(r, f"select {table}")
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        start += page_size


def insert(table, row, on_conflict=None):
    """Insert (or upsert on `on_conflict`) one row; return it."""
    params = {"on_conflict": on_conflict} if on_conflict else None
    prefer = "return=representation"
    if on_conflict:
        prefer += ",resolution=merge-duplicates"
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", params=params,
                      headers=_headers({"Prefer": prefer}), json=row, timeout=30)
    _check(r, f"insert {table}")
    return r.json()[0]


def update(table, row_id, fields):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}",
                       params={"id": f"eq.{row_id}"},
                       headers=_headers({"Prefer": "return=representation"}),
                       json=fields, timeout=30)
    _check(r, f"update {table}")
    return r.json()


def rpc(fn, args):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{fn}", headers=_headers(),
                      json=args, timeout=30)
    _check(r, f"rpc {fn}")
    return r.json() if r.content else None  # void functions return no body


def in_list(values):
    """PostgREST `in.(...)` filter value, quoting each item."""
    return "in.(" + ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values) + ")"


# ------------------------------------------------------------- storage
def upload_resume(data, filename, content_type="application/pdf"):
    """Store a résumé in the private bucket; return its object path."""
    # Storage keys must be URL-safe; the original name lives in resume_filename.
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "resume.pdf"
    path = f"{uuid.uuid4()}/{safe}"
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{RESUME_BUCKET}/{quote(path)}",
        headers={"apikey": _secret_key(), "Authorization": f"Bearer {_secret_key()}",
                 "Content-Type": content_type, "x-upsert": "false"},
        data=data, timeout=60)
    _check(r, "resume upload")
    return path


# ---------------------------------------------------------------- locks
class JobLock:
    """Lease-based run lock: `with JobLock("indeed-pipeline", 840) as got:`."""

    def __init__(self, name, ttl_seconds):
        self.name, self.ttl = name, ttl_seconds
        self.holder = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.held = False

    def __enter__(self):
        self.held = bool(rpc("try_job_lock", {"p_name": self.name,
                                              "p_holder": self.holder,
                                              "p_ttl_seconds": self.ttl}))
        return self.held

    def __exit__(self, *exc):
        if self.held:
            try:
                rpc("release_job_lock", {"p_name": self.name, "p_holder": self.holder})
            except Exception as e:  # the lease expires on its own anyway
                print(f"  ! lock release failed: {e}")
        return False
