#!/usr/bin/env python3
"""Revenue by payment method from the BigCommerce Orders API.

BigCommerce has no aggregate "revenue by payment method" endpoint, so this
walks the v2 Orders API for a date range and sums the orders itself.

Credentials come from a .env file beside this script or at the repo root (or
from real environment variables, which win). .env is gitignored — keep it that
way, and never commit the token:

    BC_STORE_HASH=abc123
    BC_ACCESS_TOKEN=...               # store-level API account, scope: Orders read-only

Usage:

    python3 revenue_by_payment_method.py --year 2026
    python3 revenue_by_payment_method.py --start 2026-01-01 --end 2026-07-01 --csv out.csv
    python3 revenue_by_payment_method.py --year 2026 --by-channel

Run it once per storefront if .com / .ca / EU live in separate BigCommerce
stores; pass --label to tag the output.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from decimal import Decimal

API_HOST = "https://api.bigcommerce.com"
PAGE_SIZE = 250

# https://developer.bigcommerce.com/docs/rest-management/orders#order-status
STATUS_NAMES = {
    0: "Incomplete",
    1: "Pending",
    2: "Shipped",
    3: "Partially Shipped",
    4: "Refunded",
    5: "Cancelled",
    6: "Declined",
    7: "Awaiting Payment",
    8: "Awaiting Pickup",
    9: "Awaiting Shipment",
    10: "Completed",
    11: "Awaiting Fulfillment",
    12: "Manual Verification Required",
    13: "Disputed",
    14: "Partially Refunded",
}

# Carts that never became orders, and orders that never took money.
DEFAULT_EXCLUDED_STATUSES = (0, 5, 6)


def load_dotenv():
    """Read KEY=VALUE from .env next to this script, then the repo root.

    Real environment variables win, so `BC_STORE_HASH=x python3 ...` still works.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(os.path.dirname(here), ".env")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)


class BigCommerceError(RuntimeError):
    pass


def request_json(path, params, store_hash, token):
    """GET a v2 endpoint, retrying on rate limit. Returns None on 204."""
    url = f"{API_HOST}/stores/{store_hash}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "X-Auth-Token": token,
            "Accept": "application/json",
        },
    )

    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 204:  # no more pages
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # BigCommerce tells us exactly how long the window has left.
                reset_ms = exc.headers.get("X-Rate-Limit-Time-Reset-Ms")
                wait = (int(reset_ms) / 1000.0) if reset_ms else 2 ** attempt
                print(f"  rate limited, sleeping {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if exc.code in (500, 502, 503, 504) and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            body = exc.read().decode("utf-8", "replace")[:500]
            raise BigCommerceError(f"HTTP {exc.code} on {path}: {body}") from exc
        except urllib.error.URLError as exc:
            if attempt < 5:
                time.sleep(2 ** attempt)
                continue
            raise BigCommerceError(f"network error on {path}: {exc.reason}") from exc

    raise BigCommerceError(f"gave up after retries on {path}")


def iter_orders(store_hash, token, min_date, max_date):
    """Yield every order created in [min_date, max_date)."""
    page = 1
    while True:
        params = {
            "limit": PAGE_SIZE,
            "page": page,
            "sort": "date_created:asc",
            "min_date_created": min_date,
            "max_date_created": max_date,
        }
        batch = request_json("/v2/orders", params, store_hash, token)
        if not batch:
            return
        for order in batch:
            yield order
        if len(batch) < PAGE_SIZE:
            return
        page += 1


def dec(value):
    """BigCommerce returns money as strings; missing/empty means zero."""
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def summarize(orders, excluded_statuses, by_channel):
    """Aggregate orders into {(currency, method[, channel]): totals}."""
    buckets = defaultdict(
        lambda: {"orders": 0, "gross": Decimal("0"), "refunded": Decimal("0")}
    )
    skipped = defaultdict(int)
    scanned = 0

    for order in orders:
        scanned += 1
        status_id = int(order.get("status_id", -1))
        if status_id in excluded_statuses:
            skipped[STATUS_NAMES.get(status_id, str(status_id))] += 1
            continue

        method = (order.get("payment_method") or "").strip() or "(none recorded)"
        currency = order.get("currency_code") or "?"
        key = (currency, method, order.get("channel_id")) if by_channel else (currency, method)

        bucket = buckets[key]
        bucket["orders"] += 1
        bucket["gross"] += dec(order.get("total_inc_tax"))
        bucket["refunded"] += dec(order.get("refunded_amount"))

    return buckets, skipped, scanned


def print_report(buckets, by_channel, label):
    if not buckets:
        print("No orders matched the filters.")
        return

    # One table per currency — mixing CAD and EUR into one total is meaningless.
    by_currency = defaultdict(list)
    for key, totals in buckets.items():
        by_currency[key[0]].append((key, totals))

    for currency in sorted(by_currency):
        rows = by_currency[currency]
        net_total = sum(t["gross"] - t["refunded"] for _, t in rows)
        order_total = sum(t["orders"] for _, t in rows)

        width = 102 if by_channel else 96
        heading = f"{label + ' — ' if label else ''}{currency}"
        print(f"\n{heading}")
        print("-" * width)
        header = f"{'Payment method':<34}"
        if by_channel:
            header += f"{'Chan':>6}"
        header += f"{'Orders':>8}{'Gross':>16}{'Refunded':>14}{'Net':>16}{'% net':>8}"
        print(header)
        print("-" * width)

        for key, totals in sorted(rows, key=lambda r: r[1]["gross"] - r[1]["refunded"], reverse=True):
            net = totals["gross"] - totals["refunded"]
            share = (net / net_total * 100) if net_total else Decimal("0")
            line = f"{key[1][:33]:<34}"
            if by_channel:
                line += f"{str(key[2] or '-'):>6}"
            line += (
                f"{totals['orders']:>8}"
                f"{totals['gross']:>16,.2f}"
                f"{totals['refunded']:>14,.2f}"
                f"{net:>16,.2f}"
                f"{share:>7.1f}%"
            )
            print(line)

        print("-" * width)
        footer = f"{'TOTAL':<34}"
        if by_channel:
            footer += f"{'':>6}"
        gross_total = sum(t["gross"] for _, t in rows)
        refunded_total = sum(t["refunded"] for _, t in rows)
        footer += (
            f"{order_total:>8}{gross_total:>16,.2f}"
            f"{refunded_total:>14,.2f}{net_total:>16,.2f}{100.0:>7.1f}%"
        )
        print(footer)


def write_csv(path, buckets, by_channel, label):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        columns = ["store", "currency", "payment_method"]
        if by_channel:
            columns.append("channel_id")
        columns += ["orders", "gross_inc_tax", "refunded", "net"]
        writer.writerow(columns)

        for key, totals in sorted(buckets.items()):
            row = [label, key[0], key[1]]
            if by_channel:
                row.append(key[2] or "")
            row += [
                totals["orders"],
                f"{totals['gross']:.2f}",
                f"{totals['refunded']:.2f}",
                f"{totals['gross'] - totals['refunded']:.2f}",
            ]
            writer.writerow(row)
    print(f"\nWrote {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, help="Calendar year, e.g. 2026 (UTC).")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (inclusive).")
    parser.add_argument("--end", help="End date YYYY-MM-DD (exclusive).")
    parser.add_argument("--label", default="", help="Name for this store in the output, e.g. 'superhairpieces.com'.")
    parser.add_argument("--csv", help="Also write the aggregates to this CSV path.")
    parser.add_argument("--by-channel", action="store_true", help="Break the table down by channel_id too.")
    parser.add_argument(
        "--include-all-statuses",
        action="store_true",
        help="Include Incomplete/Cancelled/Declined orders (excluded by default).",
    )
    args = parser.parse_args()

    if args.year:
        min_date = f"{args.year}-01-01T00:00:00+00:00"
        max_date = f"{args.year + 1}-01-01T00:00:00+00:00"
    elif args.start and args.end:
        min_date = f"{args.start}T00:00:00+00:00"
        max_date = f"{args.end}T00:00:00+00:00"
    else:
        parser.error("pass --year, or both --start and --end")

    load_dotenv()
    store_hash = os.environ.get("BC_STORE_HASH")
    token = os.environ.get("BC_ACCESS_TOKEN")
    if not store_hash or not token:
        missing = [n for n in ("BC_STORE_HASH", "BC_ACCESS_TOKEN") if not os.environ.get(n)]
        parser.error(
            f"missing {' and '.join(missing)} — set them in .env (repo root or "
            "bigcommerce-reports/) or export them in your shell"
        )

    excluded = () if args.include_all_statuses else DEFAULT_EXCLUDED_STATUSES

    print(f"Fetching orders {min_date} .. {max_date} from store {store_hash}", file=sys.stderr)
    try:
        buckets, skipped, scanned = summarize(
            iter_orders(store_hash, token, min_date, max_date), excluded, args.by_channel
        )
    except BigCommerceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Scanned {scanned} orders.", file=sys.stderr)
    if skipped:
        detail = ", ".join(f"{name} {count}" for name, count in sorted(skipped.items()))
        print(f"Excluded by status: {detail}", file=sys.stderr)

    print_report(buckets, args.by_channel, args.label)
    if args.csv:
        write_csv(args.csv, buckets, args.by_channel, args.label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
