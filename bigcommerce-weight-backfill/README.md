# BigCommerce weight backfill — superhairpieces.es

Backfills `weight` on products where it was left at 0 (BigCommerce stores an
unset weight as numeric `0`; there is no null state).

Run 2026-10-20 against store `qet21urb3p` (.es, **KGS**).

## What was done

- 826 products total; 385 had weight 0.
- 365 matched by SKU to `.ca` (gmosz3ja) or `.com` (cavofu), which are in
  **LBS** — values converted `lbs * 0.453592`.
- 7 remaining physical items (catalogs, price list, a conditioner, a patch,
  a sample) got the catalog-median placeholder **0.2 kg**.
- 13 service / download SKUs left at 0 deliberately — they never ship:
  the `SV*` salon services, `VideoCSHC`, `SRI`, `pdm`, `COForm`,
  `academyExtended Spanish`, `Extra Charge`.
- Result: 372 written, verified by re-read. Catalog now 813 set / 13 zero.

## Files

| File | |
|---|---|
| `superhairpieces-es-products-missing-weight.csv` | the original 385 |
| `es-weight-backfill-DRYRUN.csv` | planned change per product, with source |
| `es-weight-ROLLBACK.csv` | original + written value; `original_weight` restores prior state |

## Credentials

Token comes from Secret Manager `BIGCOMMERCE_ES_PRODUCT_TOKEN` (account
"SEO product - Manne", needs **Products: modify**). Note that secret holds the
whole pasted credentials block, not a bare token, so the script parses the
`ACCESS TOKEN:` line. Other stores' `BIGCOMMERCE_<hash>_ACCESS_TOKEN` secrets
hold a bare value.

## Not yet checked

`.nl` (1f8t0plkkw), `.fr` (1tqjsol232) and `.de` (34amlu9gm) likely share the
same gap if seeded from a common import. Those tokens are catalog read-only.
