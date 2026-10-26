# Bing indexing — sitemaps and instant URL submission

Gets the six Superhairpieces storefronts into Bing properly: each store's
sitemap registered once, and changed product URLs pushed within minutes so
price, stock and new-product changes don't wait for the next crawl.

Bing is also what Copilot reads, so this is the same lever for AI answers.

## The six stores

Each storefront is a separate BigCommerce store with its own hash and token, so
every job loops over all six. Canonical hosts differ — `.com` is **www**, the
other five are **non-www** — and a URL that 301s is a wasted submission, so the
registry in `config.py` is the single source of truth for them.

| store | hash | canonical origin | sitemap |
|---|---|---|---|
| ca  | `gmosz3ja`   | `https://superhairpieces.ca`      | `/xmlsitemap.php` |
| com | `cavofu`     | `https://www.superhairpieces.com` | `/xmlsitemap.php` |
| nl  | `1f8t0plkkw` | `https://superhairpieces.nl`      | `/xmlsitemap.php` |
| fr  | `1tqjsol232` | `https://superhairpieces.fr`      | `/xmlsitemap.php` |
| es  | `qet21urb3p` | `https://superhairpieces.es`      | `/xmlsitemap.php` |
| de  | `34amlu9gm`  | `https://superhairpieces.de`      | `/xmlsitemap.php` |

All six sitemaps were confirmed live on 2026-10-20: HTTP 200 at the canonical
host, valid `<sitemapindex>` XML, and declared in each `robots.txt`.

## Why not IndexNow

IndexNow is the obvious choice and it is **not usable on these stores today**.

The protocol requires a text file containing your key, served from the host.
A key at a non-root path only covers URLs beneath that path, so whole-store
coverage needs the file at the web root:

    https://superhairpieces.ca/<key>.txt   ->  body is exactly <key>

BigCommerce-hosted storefronts can't serve an arbitrary file at the root —
requesting one returns the themed 404 page as `text/html` (probed 2026-10-20,
both at `/` and under `/content/`). Without that file IndexNow answers `403`
and the submission is silently dropped.

So the instant-ping transport here is the **Bing Webmaster URL Submission API**
(`SubmitUrlBatch`), which needs no key file — only a verified site. It reaches
the same Bing crawl pipeline.

`indexnow.py` is written and tested-by-construction but disabled. It turns on
by setting `INDEXNOW_KEY` once a root key file becomes servable (a move to
Catalyst, or your own Cloudflare in front of the stores). It refuses to submit
until it has fetched the key file and matched the contents, because a wrong key
fails silently. Worth doing eventually: IndexNow also feeds Yandex, Seznam and
Naver.

## Setup

One credential is needed beyond the BigCommerce tokens already in place:

1. In [Bing Webmaster Tools](https://www.bing.com/webmasters), add and verify
   all six sites. **Import from Google Search Console** carries verification
   and sitemaps across in one step — far quicker than six DNS/meta rounds.
   Add each site at its canonical host exactly as in the table above.
2. Settings → API access → API key → generate.
3. Store it:
   ```
   gcloud secrets create BING_WEBMASTER_API_KEY --project shp-ai-bot-2026 --replication-policy=automatic
   printf %s "<key>" | gcloud secrets versions add BING_WEBMASTER_API_KEY --project shp-ai-bot-2026 --data-file=-
   ```
   For local runs, `BING_WEBMASTER_API_KEY=<key>` in the project-root `.env`
   instead. One key covers every site on the account.

## Use

```bash
python check_setup.py                       # which sites Bing knows, verified, sitemap registered
python submit_sitemaps.py                   # dry run
python submit_sitemaps.py --submit          # register all six sitemaps (idempotent)

python ping_changed.py                      # dry run: what changed since the last run
python ping_changed.py --submit             # submit it
python ping_changed.py --store ca --submit
python ping_changed.py --since-minutes 20 --submit   # stateless; what the cron runs
python ping_changed.py --all --store ca --submit     # seed every visible product (burns quota)
```

Every script defaults to a **dry run**; `--submit` is what sends anything.

## How change detection works

Products carry `date_modified` and the v3 catalog endpoint filters on it, so a
watermark poll catches every price, stock and new-product change regardless of
what caused it — a control-panel edit, an app, or one of our own sync jobs.
That is deliberately broader than hooking one sync. Hidden products are
skipped; submitting them wastes quota and teaches Bing nothing.

Manual runs keep a watermark in `state/watermarks.json` (gitignored) and only
advance it after a store's submissions succeed, so a failed run re-sends its
window rather than losing it. `--since-minutes` ignores the watermark entirely
and never writes it, which is what makes the scheduled run stateless.

## Scheduled run

`.github/workflows/bing-url-submission.yml` — every 15 minutes with a 20-minute
look-back, keyless GCP auth via Workload Identity Federation, credentials from
Secret Manager. The 5-minute overlap absorbs GitHub scheduler delay; a run
skipped outright loses its window, and the daily sitemap crawl is the backstop.

## Known gap

The `ES_BIGCOMMERCE_PRODUCT_ACCESS_TOKEN` in the project-root `.env` is stale —
it returns `401`. The store hash and URL logic are correct (verified against a
working token), so `.es` works as soon as a current token is in `.env` or
Secret Manager as `BIGCOMMERCE_qet21urb3p_ACCESS_TOKEN`.
