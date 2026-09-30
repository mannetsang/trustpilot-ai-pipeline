"""The one Google approval behind "Connect everything" in the Access tab.

Manne is the project's owner. When he presses Connect, Google asks him to let the
app act for him in Google Cloud once; with that short-lived token (never stored)
the app:

- gives its own service account read access to each secret a connector needs,
  one secret at a time, nothing project-wide;
- creates an empty secret for each key Manne will paste (none right now)
  and lets the app add versions to it, so a pasted key goes straight into
  Secret Manager;
- switches on the Google APIs the Google tools need (Analytics, Search Console).
"""

import json
import os

import requests

import integrations

GCP_PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
APP_SERVICE_ACCOUNT = os.environ.get("APP_SERVICE_ACCOUNT", f"chat-assistant@{GCP_PROJECT}.iam.gserviceaccount.com")
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SM = "https://secretmanager.googleapis.com/v1"
READ = "roles/secretmanager.secretAccessor"
WRITE = "roles/secretmanager.secretVersionManager"


class Owner:
    """Calls Google Cloud as Manne, with the token from this one approval."""

    def __init__(self, token, http=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.http = http or requests

    def __call__(self, method, url, **kw):
        resp = self.http.request(method, url, headers=self.headers, timeout=30, **kw)
        try:
            return resp.status_code, (json.loads(resp.text) if (resp.text or "").strip() else {})
        except ValueError:
            return resp.status_code, {}


def plan():
    """secret -> True when Manne pastes it (so the app must be able to write it), False when it only reads it."""
    wanted = {}
    for i in integrations.REGISTRY:
        for name in i.secrets + i.optional:
            wanted[name] = wanted.get(name, False) or name in i.paste_names()
    return wanted


def _grant(owner, name, member, roles):
    """Add member to each role on one secret (read-modify-write with the policy's etag)."""
    url = f"{SM}/projects/{GCP_PROJECT}/secrets/{name}"
    status, policy = owner("GET", f"{url}:getIamPolicy")
    if status != 200:
        raise RuntimeError(f"couldn't read who may use {name} (HTTP {status}: {policy.get('error', {}).get('message', '')})")
    bindings = policy.setdefault("bindings", [])
    changed = False
    for role in roles:
        binding = next((b for b in bindings if b.get("role") == role and not b.get("condition")), None)
        if binding is None:
            binding = {"role": role, "members": []}
            bindings.append(binding)
        if member not in binding["members"]:
            binding["members"].append(member)
            changed = True
    if changed:
        status, body = owner("POST", f"{url}:setIamPolicy", json={"policy": policy})
        if status != 200:
            raise RuntimeError(f"couldn't give the app access to {name} (HTTP {status}: {body.get('error', {}).get('message', '')})")
    return changed


def run(token, http=None):
    """Grant, create and switch on everything the connectors need. Returns what was done, per step."""
    owner = Owner(token, http)
    member = f"serviceAccount:{APP_SERVICE_ACCOUNT}"
    report = {"granted": [], "already": [], "created": [], "missing": [], "enabled": [], "errors": []}
    for name, pasted in sorted(plan().items()):
        try:
            status, _ = owner("GET", f"{SM}/projects/{GCP_PROJECT}/secrets/{name}")
            if status == 404:
                if not pasted:
                    report["missing"].append(name)  # a key that should exist but doesn't: nothing to grant
                    continue
                status, body = owner("POST", f"{SM}/projects/{GCP_PROJECT}/secrets", params={"secretId": name},
                                     json={"replication": {"automatic": {}}, "labels": {"managed-by": "chat-assistant"}})
                if status != 200:
                    raise RuntimeError(f"couldn't create {name} (HTTP {status}: {body.get('error', {}).get('message', '')})")
                report["created"].append(name)
            elif status != 200:
                raise RuntimeError(f"couldn't look up {name} (HTTP {status})")
            changed = _grant(owner, name, member, [READ, WRITE] if pasted else [READ])
            report["granted" if changed else "already"].append(name)
        except Exception as exc:  # noqa: BLE001 - one secret's problem mustn't stop the rest
            report["errors"].append(f"{name}: {str(exc)[:200]}")
    services = sorted({svc for i in integrations.REGISTRY for svc in i.services})
    if services:
        status, body = owner("POST", f"https://serviceusage.googleapis.com/v1/projects/{GCP_PROJECT}/services:batchEnable",
                             json={"serviceIds": services})
        if status == 200:
            report["enabled"] = services
        else:
            report["errors"].append(f"switching on {', '.join(services)}: HTTP {status} "
                                    f"{body.get('error', {}).get('message', '')}"[:300])
    return report
