"""
Register a Trustpilot webhook pointing at your Apps Script Web App URL.

Steps before running:
  1. In Apps Script: Deploy → New deployment → Web App
     - Execute as: Me
     - Who has access: Anyone
     - Copy the /exec URL
  2. Paste it as APPS_SCRIPT_URL below (or set env var APPS_SCRIPT_URL)
  3. Run: python register_webhook.py
"""

import os
import base64
import requests

API_KEY    = "tpk-LagHAs25A3IVuj1ud2PFj8QpuDPu"
SECRET     = "tps-Sf2deEEUCfX7"
BU_ID      = "5e44f707d7d8c700011eaa10"   # superhairpieces.com

# Paste your deployed Apps Script /exec URL here
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL", "https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec")


def get_access_token():
    credentials = base64.b64encode(f"{API_KEY}:{SECRET}".encode()).decode()
    r = requests.post(
        "https://api.trustpilot.com/v1/oauth/oauth-business-users-for-applications/accesstoken",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def list_webhooks(token):
    r = requests.get(
        "https://api.trustpilot.com/v1/private/webhooks",
        headers={"Authorization": f"Bearer {token}"},
        params={"apikey": API_KEY},
    )
    print(f"List webhooks: {r.status_code}")
    print(r.text)
    return r


def register_webhook(token, url):
    r = requests.post(
        "https://api.trustpilot.com/v1/private/webhooks",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        params={"apikey": API_KEY},
        json={
            "url": url,
            "events": ["review.created"],
        },
    )
    print(f"Register webhook: {r.status_code}")
    print(r.text)
    return r


if __name__ == "__main__":
    if "YOUR_DEPLOYMENT_ID" in APPS_SCRIPT_URL:
        print("ERROR: Set APPS_SCRIPT_URL to your deployed Apps Script /exec URL first.")
        raise SystemExit(1)

    print("Getting access token...")
    token = get_access_token()
    print(f"Token: {token[:20]}...")

    print("\nExisting webhooks:")
    list_webhooks(token)

    print(f"\nRegistering webhook → {APPS_SCRIPT_URL}")
    register_webhook(token, APPS_SCRIPT_URL)
