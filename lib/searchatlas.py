"""
Search Atlas API client: third-party SEO estimates for any domain.

One key, `SEARCHATLAS_API_KEY` in Secret Manager (env `SEARCHATLAS_API_KEY`),
sent as an `X-API-Key` header. The platform is split across hosts, one per
service, and a call to the wrong host answers 404 rather than redirecting:

    api.searchatlas.com       core: indexing batches, projects, quota
    keyword.searchatlas.com   Keyword Explorer: bulk domain metrics, gap analysis
    sa.searchatlas.com        Site Auditor and Site Explorer projects
    gsc.searchatlas.com       Search Console-backed reports
    backlink.searchatlas.com  backlink research

The full list of 798 endpoints is the OpenAPI file at
https://docs.searchatlas.com/searchatlas-api.json.

The traffic numbers are modelled estimates, the same kind Semrush and Ahrefs
show, not analytics. For our own six storefronts Search Console (the
`google-search-console/` collector) is the accurate source; this is for
competitors and for a quick relative read across markets.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.secrets import get_secret  # noqa: E402

SECRET_ID = "SEARCHATLAS_API_KEY"
ENV_VAR = "SEARCHATLAS_API_KEY"

HOSTS = {
    "core": "https://api.searchatlas.com",
    "keyword": "https://keyword.searchatlas.com",
    "site": "https://sa.searchatlas.com",
    "gsc": "https://gsc.searchatlas.com",
    "backlink": "https://backlink.searchatlas.com",
}


class SearchAtlasError(RuntimeError):
    pass


def _key() -> str:
    return get_secret(SECRET_ID, env_var=ENV_VAR)


def request(service: str, path: str, body=None, method=None, timeout=60):
    """Call one endpoint. `service` picks the host from HOSTS; `path` starts with /."""
    url = HOSTS[service] + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method or ("POST" if data else "GET"),
        headers={
            "X-API-Key": _key(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Cloudflare in front of Search Atlas answers 403 (error 1010) to
            # the default Python-urllib user agent, so identify ourselves.
            "User-Agent": "superhairpieces-pipeline/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SearchAtlasError(f"{req.get_method()} {url} -> HTTP {exc.code}: {detail}") from exc


def bulk_url_metrics(domains):
    """Domain authority, estimated organic traffic and backlink counts per domain.

    POST keyword.searchatlas.com/api/v1/bulk-url-metrics/. Returns the API's
    list of dicts; the fields that matter are `traffic` (estimated monthly
    organic visits), `keywords_count`, `domain_rating`, `domains_count`
    (referring domains) and `backlinks_count`. Hosts are reported bare,
    e.g. `www.superhairpieces.com` comes back as `superhairpieces.com`.
    """
    return request("keyword", "/api/v1/bulk-url-metrics/", {"urls": list(domains)})


def main(argv):
    if len(argv) < 2:
        from lib.stores import STORES
        domains = [s.domain for s in STORES]
    else:
        domains = argv[1:]
    rows = bulk_url_metrics(domains)
    print(f"{'domain':28} {'traffic':>8} {'keywords':>9} {'DR':>4} {'ref doms':>9} {'backlinks':>10}")
    for r in rows:
        print(f"{r['url']:28} {r.get('traffic') or 0:8} {r.get('keywords_count') or 0:9} "
              f"{r.get('domain_rating') or 0:4} {r.get('domains_count') or 0:9} {r.get('backlinks_count') or 0:10}")


if __name__ == "__main__":
    main(sys.argv)
