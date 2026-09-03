"""Import the register's product list from the show-prep Google Sheet.

Reads one tab (default 所有拿货及定价, "everything sourced and its pricing")
of the spreadsheet as the session/CI service account, which the sheet must be
shared with, and upserts it into pos_products and pos_product_barcodes in
Supabase. Re-running is safe: rows are matched on SKU, and only the columns
that come from the sheet are overwritten.

How the sheet is read:

  - The header row is found by looking for a cell that says SKU; the totals
    line above it is skipped.
  - NAME is 'ESI Montreal - <SKU>' in this sheet, so the event prefix is
    dropped and name equals the SKU. A proper name column would flow through.
  - 蒙特利尔定价 (Montreal price) is the selling price, tax included. A blank
    price makes the product inactive: it cannot be rung up.
  - 本次展会拿货数量 (quantity brought) is parsed leniently: '2+1*' becomes
    3 with the raw text kept; '*' alone becomes null.
  - The same SKU on several rows is one product with several barcodes; its
    quantities are summed and the first row's other fields win. Differing
    prices for one SKU are reported.
  - UPC, EAN or ASIN codes all go into pos_product_barcodes as typed (trimmed).

Usage (one line, Windows cmd):

    python pos/import_products.py --dry-run     read and report, write nothing
    python pos/import_products.py               upsert into Supabase
    python pos/import_products.py --deactivate-missing
                                                also mark SKUs absent from the sheet inactive

Connection: same rules as apply_migration.py (--via api|db|auto). Google
access uses Application Default Credentials with a read-only Sheets scope:
the service-account key on a cloud session or in CI, or `gcloud auth
application-default login --scopes=...` on your own machine. Needs the
google-auth package (installed with google-cloud-secret-manager).
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apply_migration import connect  # noqa: E402

DEFAULT_SHEET_ID = "1G9lpa6kXEsyOcON3yiEZavh8ZtCvZuz7KSmfrAyRT9Y"
DEFAULT_TAB = "所有拿货及定价"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
USER_AGENT = "trustpilot-ai-pipeline pos/import_products"

# Sheet header -> meaning. Headers are matched exactly after trimming.
COL = {
    "category": "类别",
    "name": "NAME",
    "sku": "SKU",
    "upc": "UPC",
    "qty": "本次展会拿货数量",
    "cost": "成本",
    "price": "蒙特利尔定价",
    "note": "Note",
    "clearance": "清仓",
}
NAME_PREFIX = re.compile(r"^ESI\s+\w+\s*-\s*", re.IGNORECASE)
BATCH = 100


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


def sheet_values(sheet_id, tab):
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
    credentials.refresh(Request())
    quoted = urllib.parse.quote(f"'{tab}'", safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quoted}"
        "?valueRenderOption=FORMATTED_VALUE"
    )
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {credentials.token}", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8")).get("values", [])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def money(text):
    """'CA$2,384.00' -> '2384.00'; '' -> None. Anything else raises."""
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    match = re.fullmatch(r"(?:CA\$|US\$|€|\$)?(-?\d+(?:\.\d+)?)", text)
    if not match:
        raise ValueError(f"not a money value: {text!r}")
    return f"{float(match.group(1)):.2f}"


def quantity(text):
    """'20' -> 20, '2+1*' -> 3, '*' -> None, '-1' -> -1. Returns (int_or_None, raw_or_None)."""
    raw = (text or "").strip()
    if not raw:
        return None, None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw), None
    numbers = re.findall(r"\d+", raw)
    return (sum(int(n) for n in numbers) if numbers else None), raw


def parse(values):
    """Return (products, problems). products: OrderedDict sku -> dict."""
    header_index = next(
        (i for i, row in enumerate(values) if any(c.strip() == COL["sku"] for c in row)), None
    )
    if header_index is None:
        raise SystemExit(f"no header row with a {COL['sku']!r} column found")
    header = [c.strip() for c in values[header_index]]
    idx = {}
    for key, title in COL.items():
        if title not in header:
            raise SystemExit(f"column {title!r} ({key}) missing from the header row")
        idx[key] = header.index(title)

    def cell(row, key):
        i = idx[key]
        return row[i].strip() if i < len(row) else ""

    products = OrderedDict()
    problems = []
    for offset, row in enumerate(values[header_index + 1 :], start=header_index + 2):
        if not any(c.strip() for c in row):
            continue
        sku = cell(row, "sku")
        if not sku:
            problems.append(f"row {offset}: blank SKU, skipped")
            continue
        try:
            price = money(cell(row, "price"))
            cost = money(cell(row, "cost"))
        except ValueError as exc:
            problems.append(f"row {offset} {sku}: {exc}, skipped")
            continue
        qty, qty_raw = quantity(cell(row, "qty"))
        name = NAME_PREFIX.sub("", cell(row, "name")) or sku
        barcode = cell(row, "upc")
        raw = {header[i]: (row[i] if i < len(row) else "") for i in range(len(header)) if header[i]}

        record = {
            "sku": sku,
            "name": name,
            "category": cell(row, "category") or "Uncategorised",
            "price": price,
            "cost": cost,
            "show_qty": qty,
            "show_qty_raw": qty_raw,
            "is_set": cell(row, "note").lower() == "set",
            "is_clearance": cell(row, "clearance").upper() == "TRUE",
            "is_active": price is not None,
            "note": cell(row, "note") or None,
            "barcodes": [barcode] if barcode else [],
            "sheet": raw,
            "sheet_row": offset,
        }
        if price is None:
            problems.append(f"row {offset} {sku}: no price, imported as inactive")

        if sku in products:
            first = products[sku]
            if barcode and barcode not in first["barcodes"]:
                first["barcodes"].append(barcode)
            if first["show_qty"] is not None and qty is not None:
                first["show_qty"] += qty
            if first["price"] != price:
                problems.append(
                    f"row {offset} {sku}: duplicate SKU with a different price "
                    f"({first['price']} vs {price}); keeping the first"
                )
            else:
                problems.append(f"row {offset} {sku}: duplicate SKU merged into row {first['sheet_row']}")
            continue
        products[sku] = record

    # A barcode must point at one product.
    seen = {}
    for sku, record in products.items():
        for code in list(record["barcodes"]):
            if code in seen and seen[code] != sku:
                problems.append(f"barcode {code} is on both {seen[code]} and {sku}; left on {seen[code]}")
                record["barcodes"].remove(code)
            else:
                seen[code] = sku
    return products, problems


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def lit(value):
    """A SQL literal. The Management API takes raw SQL, so values are inlined."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return lit(json.dumps(value, ensure_ascii=False)) + "::jsonb"
    return "'" + str(value).replace("'", "''") + "'"


PRODUCT_COLUMNS = [
    "sku", "name", "category", "price", "cost", "show_qty", "show_qty_raw",
    "is_set", "is_clearance", "is_active", "note", "sheet", "sheet_row", "imported_at",
]


def upsert_sql(batch, imported_at):
    rows = []
    for record in batch:
        values = dict(record, imported_at=imported_at)
        rows.append("(" + ", ".join(lit(values[c]) for c in PRODUCT_COLUMNS) + ")")
    updates = ", ".join(f"{c} = excluded.{c}" for c in PRODUCT_COLUMNS if c != "sku")
    sql = (
        f"insert into pos_products ({', '.join(PRODUCT_COLUMNS)}) values\n"
        + ",\n".join(rows)
        + f"\non conflict (sku) do update set {updates};\n"
    )
    codes = [(code, r["sku"]) for r in batch for code in r["barcodes"]]
    if codes:
        pairs = ",\n".join(f"({lit(code)}, {lit(sku)})" for code, sku in codes)
        sql += (
            "insert into pos_product_barcodes (barcode, product_id)\n"
            f"select v.barcode, p.id from (values\n{pairs}\n) as v (barcode, sku)\n"
            "join pos_products p on p.sku = v.sku\n"
            "on conflict (barcode) do update set product_id = excluded.product_id;\n"
        )
    return sql


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    parser.add_argument("--tab", default=DEFAULT_TAB)
    parser.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    parser.add_argument("--deactivate-missing", action="store_true",
                        help="set is_active = false on products whose SKU is not in the sheet")
    parser.add_argument("--via", choices=["auto", "api", "db"], default="auto")
    args = parser.parse_args()

    values = sheet_values(args.sheet_id, args.tab)
    products, problems = parse(values)

    active = sum(1 for p in products.values() if p["is_active"])
    barcodes = sum(len(p["barcodes"]) for p in products.values())
    categories = {}
    for p in products.values():
        categories[p["category"]] = categories.get(p["category"], 0) + 1
    print(f"sheet tab {args.tab!r}: {len(values)} rows read")
    print(f"products: {len(products)} ({active} active, {len(products) - active} inactive), barcodes: {barcodes}")
    print("categories: " + ", ".join(f"{k} {v}" for k, v in sorted(categories.items())))
    for problem in problems:
        print(f"  note: {problem}")
    if args.dry_run:
        print("dry run, nothing written")
        return

    backend = connect(args.via)
    imported_at = datetime.now(timezone.utc).isoformat()
    records = list(products.values())
    try:
        for start in range(0, len(records), BATCH):
            batch = records[start : start + BATCH]
            backend.run(upsert_sql(batch, imported_at))
            print(f"upserted {min(start + BATCH, len(records))}/{len(records)}", flush=True)
        if args.deactivate_missing:
            skus = ", ".join(lit(s) for s in products)
            rows = backend.run(
                f"update pos_products set is_active = false where is_active and sku not in ({skus}) returning sku;"
            )
            print(f"deactivated {len(rows)} product(s) not in the sheet")
        totals = backend.run(
            "select (select count(*) from pos_products) as products,"
            " (select count(*) from pos_products where is_active) as active,"
            " (select count(*) from pos_product_barcodes) as barcodes;"
        )
        print("database now:", totals[0] if totals else "?")
    finally:
        backend.close()


if __name__ == "__main__":
    main()
