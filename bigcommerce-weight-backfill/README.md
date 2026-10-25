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

`apply_weights.py` reads its token from a local file, `$SP/estok2.txt` - not
from Secret Manager. It accepts either a bare token or a pasted credentials
block, taking the `ACCESS TOKEN:` line from the latter.

This file used to say the token came from Secret Manager
`BIGCOMMERCE_ES_PRODUCT_TOKEN`. That was never true of the script, and that
secret has since been deleted: it held a pasted block whose token no longer
authenticated, and nothing read it.

The account needs **Products: modify**. If this script is ever run again it
should take the token the same way everything else does - `get_secret` on
`BIGCOMMERCE_qet21urb3p_ACCESS_TOKEN`, via `lib/stores.py`, which is a bare
token and is the one kept current. A credential in a working file beside the
data is how the `.es` token came to be pasted into the wrong place to begin
with.

## Not yet checked

`.nl` (1f8t0plkkw), `.fr` (1tqjsol232) and `.de` (34amlu9gm) likely share the
same gap if seeded from a common import. Those tokens are catalog read-only.
