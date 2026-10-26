"""
IndexNow transport - built, but OFF until a key file can be hosted.

IndexNow needs a text file containing the key, served from the host. Per the
protocol, a key at a non-root path only covers URLs under that path, so for
whole-store coverage the file must sit at the web root:

    https://superhairpieces.ca/<key>.txt   ->  body is exactly <key>

BigCommerce-hosted storefronts cannot serve an arbitrary file at the root - a
request for one returns the themed 404 page as text/html (probed 2026-10-20).
So this module stays disabled and ping_changed.py uses the Bing Webmaster
SubmitUrlBatch transport instead, which needs no key file.

Turn it on by setting INDEXNOW_KEY (and INDEXNOW_KEY_LOCATION_<STORE> if the
file is not at <origin>/<key>.txt) once the root file is servable - e.g. after
a move to Catalyst, or behind a Cloudflare account you control. preflight()
refuses to submit until it has actually fetched the file and matched the key,
because a bad key silently burns submissions with a 403.
"""
import json
import os
import urllib.error
import urllib.request

ENDPOINT = "https://api.indexnow.org/indexnow"
MAX_URLS_PER_REQUEST = 10_000
TIMEOUT = 45

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

STATUS_MEANING = {
    200: "ok",
    202: "accepted, key validation pending",
    400: "bad request (invalid format)",
    403: "key not valid (key file missing or contents do not match)",
    422: "URLs do not belong to the host, or key does not match",
    429: "rate limited",
}


class IndexNowDisabled(RuntimeError):
    pass


def key_for(store) -> str | None:
    return os.environ.get("INDEXNOW_KEY") or None


def key_location_for(store, key: str) -> str:
    override = os.environ.get(f"INDEXNOW_KEY_LOCATION_{store.key.upper()}")
    return override or f"{store.origin}/{key}.txt"


def preflight(store) -> tuple[str, str]:
    """Return (key, key_location) once the key file is confirmed servable.

    Raises IndexNowDisabled with a precise reason otherwise. Never submits on a
    guess: a 403 from IndexNow is silent as far as the store is concerned.
    """
    key = key_for(store)
    if not key:
        raise IndexNowDisabled("INDEXNOW_KEY is not set")

    location = key_location_for(store, key)
    req = urllib.request.Request(location, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        raise IndexNowDisabled(f"key file {location} -> HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise IndexNowDisabled(f"key file {location} unreachable: {exc.reason}") from None

    if body != key:
        raise IndexNowDisabled(
            f"key file {location} does not contain the key "
            f"(got {len(body)} bytes, content-type {ctype or 'unknown'})"
        )
    if "text/plain" not in ctype:
        raise IndexNowDisabled(
            f"key file {location} is served as {ctype or 'unknown'}, not text/plain"
        )
    return key, location


def submit(store, urls: list[str]) -> list[tuple[int, str]]:
    """Submit URLs for one store. Returns (status, meaning) per request sent."""
    key, location = preflight(store)
    host = store.origin.split("://", 1)[1]

    results = []
    for i in range(0, len(urls), MAX_URLS_PER_REQUEST):
        chunk = urls[i:i + MAX_URLS_PER_REQUEST]
        payload = json.dumps({
            "host": host,
            "key": key,
            "keyLocation": location,
            "urlList": chunk,
        }).encode("utf-8")
        req = urllib.request.Request(
            ENDPOINT, data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except urllib.error.URLError as exc:
            results.append((0, f"unreachable: {exc.reason}"))
            continue
        results.append((status, STATUS_MEANING.get(status, "unexpected status")))
    return results
