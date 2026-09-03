"""Superhairpieces POS: the cash register for the ESI Montreal booth.

Serves the register page (static/) and a small JSON API over the Supabase
Postgres database. Cash sales only. Every sale is written as one
pos_transactions row plus its pos_transaction_items, with the totals checked
again on the server so the browser can never ring a wrong amount.

Env vars:
  SUPABASE_DB_URL     Postgres connection string. If unset, read from Secret
                      Manager (secret SUPABASE_DB_URL) with the runtime identity.
  POS_ACCESS_CODE     Code staff type to unlock the till. If unset, read from
                      Secret Manager (secret POS_ACCESS_CODE) and re-read every
                      minute, so it can be created or rotated without a deploy.
                      Until it exists the till stays locked.
  POS_LOCATION_ID     Written on every sale. Default esi-montreal-2026.
  POS_LOCATION_NAME   Shown in the header. Default ESI Montreal 2026.
  POS_CURRENCY        Default CAD.
  POS_TAX_RATE        Tax included in the shelf price, backed out for the
                      receipt. Default 0.14975 (GST 5% + QST 9.975%).
  POS_TAX_LABEL       Default "GST 5% + QST 9.975%".
  GCP_PROJECT         Secret Manager project. Default shp-ai-bot-2026.
"""

import hashlib
import hmac
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

import psycopg
from flask import Flask, abort, jsonify, request, send_from_directory, session
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

app = Flask(__name__, static_folder="static", static_url_path="/static")

PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
LOCATION_ID = os.environ.get("POS_LOCATION_ID", "esi-montreal-2026")
LOCATION_NAME = os.environ.get("POS_LOCATION_NAME", "ESI Montreal 2026")
CURRENCY = os.environ.get("POS_CURRENCY", "CAD")
TAX_RATE = Decimal(os.environ.get("POS_TAX_RATE", "0.14975"))
TAX_LABEL = os.environ.get("POS_TAX_LABEL", "GST 5% + QST 9.975%")
CENT = Decimal("0.01")

# ---------------------------------------------------------------------------
# Secrets and configuration
# ---------------------------------------------------------------------------

_secret_cache = {}


def secret(name, ttl=60):
    """Env var first, then Secret Manager, cached for ttl seconds. None if absent."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    hit = _secret_cache.get(name)
    if hit and hit[0] > time.time():
        return hit[1]
    value = None
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(
            request={"name": f"projects/{PROJECT}/secrets/{name}/versions/latest"}
        )
        value = response.payload.data.decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001 - absence is a normal state here
        app.logger.warning("secret %s unavailable: %s", name, type(exc).__name__)
    _secret_cache[name] = (time.time() + ttl, value)
    return value


def access_code():
    return secret("POS_ACCESS_CODE")


# The Flask session cookie is signed with a key derived from the database URL,
# so it survives restarts and is the same across instances.
_db_url = secret("SUPABASE_DB_URL", ttl=3600)
app.secret_key = hashlib.sha256(("pos-session:" + (_db_url or uuid.uuid4().hex)).encode()).digest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("K_SERVICE") is not None,
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 14)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()


def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            url = secret("SUPABASE_DB_URL", ttl=3600)
            if not url:
                abort(503, "database not configured")
            # Supabase's pooler runs in transaction mode: no server-side
            # prepared statements.
            _pool = ConnectionPool(
                url, min_size=0, max_size=4, open=True, kwargs={"prepare_threshold": None, "row_factory": dict_row}
            )
        return _pool


def query(sql, params=None, one=False):
    with pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return (rows[0] if rows else None) if one else rows


# ---------------------------------------------------------------------------
# Auth: one shared access code unlocks the till for a shift
# ---------------------------------------------------------------------------


def unlocked():
    return bool(session.get("unlocked"))


def require_unlock(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not unlocked():
            abort(401, "till is locked")
        return fn(*args, **kwargs)
    return wrapper


def code_matches(candidate):
    expected = access_code()
    return bool(expected) and hmac.compare_digest(candidate.strip(), expected)


@app.get("/api/config")
def config():
    return jsonify({
        "locationId": LOCATION_ID, "locationName": LOCATION_NAME, "currency": CURRENCY,
        "taxRate": str(TAX_RATE), "taxLabel": TAX_LABEL, "pricesIncludeTax": True,
        "unlocked": unlocked(), "staff": session.get("staff"),
        "configured": access_code() is not None,
    })


@app.post("/api/unlock")
def unlock():
    body = request.get_json(silent=True) or {}
    if access_code() is None:
        abort(503, "access code not configured yet")
    if not code_matches(str(body.get("code", ""))):
        time.sleep(0.5)
        abort(403, "wrong code")
    staff = re.sub(r"\s+", " ", str(body.get("staff", ""))).strip()[:60]
    if not staff:
        abort(400, "staff name required")
    session.permanent = True
    session["unlocked"] = True
    session["staff"] = staff
    return jsonify({"ok": True, "staff": staff})


@app.post("/api/lock")
def lock():
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/staff")
@require_unlock
def set_staff():
    staff = re.sub(r"\s+", " ", str((request.get_json(silent=True) or {}).get("staff", ""))).strip()[:60]
    if not staff:
        abort(400, "staff name required")
    session["staff"] = staff
    return jsonify({"ok": True, "staff": staff})


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

PRODUCT_COLS = "p.id, p.sku, p.name, p.category, p.price, p.is_set, p.is_clearance, p.show_qty"


@app.get("/api/products")
@require_unlock
def products():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    where = ["p.is_active", "p.price is not null"]
    params = []
    if category and category != "all":
        where.append("p.category = %s")
        params.append(category)
    if q:
        where.append("(p.name ilike %s or p.sku ilike %s or exists (select 1 from pos_product_barcodes b where b.product_id = p.id and b.barcode = %s))")
        params += [f"%{q}%", f"%{q}%", q]
    rows = query(
        f"select {PRODUCT_COLS} from pos_products p where {' and '.join(where)} order by p.category, p.name limit 300",
        params,
    )
    return jsonify([serialise_product(r) for r in rows])


@app.get("/api/categories")
@require_unlock
def categories():
    rows = query("select category, count(*) as n from pos_products where is_active and price is not null group by 1 order by 1")
    return jsonify(rows)


@app.get("/api/barcode/<code>")
@require_unlock
def by_barcode(code):
    row = query(
        f"select {PRODUCT_COLS} from pos_products p join pos_product_barcodes b on b.product_id = p.id "
        "where b.barcode = %s and p.is_active and p.price is not null",
        (code.strip(),), one=True,
    )
    if not row:
        # Some codes are typed as the SKU itself.
        row = query(f"select {PRODUCT_COLS} from pos_products p where p.sku = %s and p.is_active and p.price is not null",
                    (code.strip(),), one=True)
    if not row:
        abort(404, "no product with that barcode")
    return jsonify(serialise_product(row))


def serialise_product(r):
    return {
        "id": str(r["id"]), "sku": r["sku"], "name": r["name"], "category": r["category"],
        "price": str(r["price"]), "isSet": r["is_set"], "isClearance": r["is_clearance"], "showQty": r["show_qty"],
    }


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def totals(lines, discount):
    """Tax-inclusive pricing: the shelf price is what the customer pays."""
    subtotal = sum((money(l["unit_price"]) * l["quantity"] for l in lines), Decimal("0"))
    discount = money(discount or 0)
    if discount < 0 or discount > subtotal:
        abort(400, "discount out of range")
    grand = money(subtotal - discount)
    tax = money(grand - grand / (1 + TAX_RATE))
    return subtotal, discount, tax, grand


@app.post("/api/sales")
@require_unlock
def create_sale():
    body = request.get_json(silent=True) or {}
    record_id = str(body.get("clientRecordId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", record_id):
        abort(400, "clientRecordId required")
    items = body.get("items") or []
    if not items:
        abort(400, "no items")

    # Price from the database, never from the browser.
    ids = [str(i.get("productId")) for i in items]
    rows = query(f"select {PRODUCT_COLS} from pos_products p where p.id::text = any(%s) and p.is_active and p.price is not null", (ids,))
    by_id = {str(r["id"]): r for r in rows}
    lines = []
    for n, item in enumerate(items, start=1):
        product = by_id.get(str(item.get("productId")))
        if not product:
            abort(400, f"unknown or inactive product on line {n}")
        qty = int(item.get("quantity") or 0)
        if qty <= 0 or qty > 999:
            abort(400, f"bad quantity on line {n}")
        lines.append({"line_no": n, "product": product, "quantity": qty, "unit_price": product["price"],
                      "barcode": (str(item.get("barcode") or "").strip() or None)})

    subtotal, discount, tax, grand = totals(lines, body.get("discount"))
    cash = money(body.get("cashReceived") or 0)
    if cash < grand:
        abort(400, "cash received is less than the total")
    change = money(cash - grand)
    note = (str(body.get("note") or "").strip() or None)

    with pool().connection() as conn:
        with conn.transaction():
            existing = conn.execute("select * from pos_transactions where client_record_id = %s", (record_id,)).fetchone()
            if existing:
                return jsonify(sale_payload(conn, existing)), 200
            txn = conn.execute(
                """insert into pos_transactions (client_record_id, location_id, staff_name, currency, payment_method,
                     prices_include_tax, subtotal, discount_total, tax_rate, tax_label, tax_total, grand_total,
                     amount_paid, cash_received, change_given, note)
                   values (%s, %s, %s, %s, 'cash', true, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   returning *""",
                (record_id, LOCATION_ID, session.get("staff") or "—", CURRENCY, subtotal, discount, TAX_RATE, TAX_LABEL,
                 tax, grand, grand, cash, change, note),
            ).fetchone()
            for l in lines:
                p = l["product"]
                conn.execute(
                    """insert into pos_transaction_items (transaction_id, line_no, sku, product_name, category, quantity,
                         unit_price, product_id, barcode)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (txn["id"], l["line_no"], p["sku"], p["name"], p["category"], l["quantity"], l["unit_price"], p["id"], l["barcode"]),
                )
            payload = sale_payload(conn, txn)
    return jsonify(payload), 201


def sale_payload(conn, txn):
    items = conn.execute(
        "select line_no, sku, product_name, category, quantity, unit_price, line_total from pos_transaction_items "
        "where transaction_id = %s order by line_no", (txn["id"],)
    ).fetchall()
    return serialise_sale(txn, items)


def serialise_sale(t, items):
    def s(v):
        return None if v is None else str(v)
    return {
        "id": str(t["id"]), "number": t["transaction_number"], "status": t["status"], "staff": t["staff_name"],
        "locationId": t["location_id"], "currency": t["currency"], "paymentMethod": t["payment_method"],
        "subtotal": s(t["subtotal"]), "discount": s(t["discount_total"]), "taxRate": s(t["tax_rate"]),
        "taxLabel": t["tax_label"], "tax": s(t["tax_total"]), "total": s(t["grand_total"]),
        "amountPaid": s(t["amount_paid"]), "cashReceived": s(t["cash_received"]), "change": s(t["change_given"]),
        "refundAmount": s(t["refund_amount"]), "refundReason": t["refund_reason"],
        "refundedAt": t["refunded_at"].isoformat() if t["refunded_at"] else None,
        "note": t["note"], "soldAt": t["sold_at"].isoformat(),
        "items": [{"lineNo": i["line_no"], "sku": i["sku"], "name": i["product_name"], "category": i["category"],
                   "quantity": i["quantity"], "unitPrice": s(i["unit_price"]), "lineTotal": s(i["line_total"])} for i in items],
    }


@app.get("/api/sales")
@require_unlock
def list_sales():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 50)), 200)
    params = [LOCATION_ID]
    where = "location_id = %s"
    if q:
        where += " and (transaction_number::text = %s or staff_name ilike %s)"
        params += [q, f"%{q}%"]
    params.append(limit)
    with pool().connection() as conn:
        txns = conn.execute(f"select * from pos_transactions where {where} order by sold_at desc limit %s", params).fetchall()
        ids = [t["id"] for t in txns]
        items = conn.execute(
            "select transaction_id, line_no, sku, product_name, category, quantity, unit_price, line_total "
            "from pos_transaction_items where transaction_id = any(%s) order by line_no", (ids,)
        ).fetchall() if ids else []
    grouped = {}
    for i in items:
        grouped.setdefault(i["transaction_id"], []).append(i)
    return jsonify([serialise_sale(t, grouped.get(t["id"], [])) for t in txns])


@app.post("/api/sales/<int:number>/refund")
@require_unlock
def refund(number):
    body = request.get_json(silent=True) or {}
    # A refund needs the access code typed again: the till's manager approval.
    if not code_matches(str(body.get("code", ""))):
        abort(403, "approval code wrong")
    reason = str(body.get("reason") or "").strip()[:200]
    if not reason:
        abort(400, "reason required")
    with pool().connection() as conn:
        with conn.transaction():
            t = conn.execute("select * from pos_transactions where transaction_number = %s and location_id = %s for update",
                             (number, LOCATION_ID)).fetchone()
            if not t:
                abort(404, "no such sale")
            if t["refund_amount"] is not None:
                abort(409, "already refunded")
            amount = money(body.get("amount") or t["amount_paid"])
            if amount <= 0 or amount > t["amount_paid"]:
                abort(400, "refund amount out of range")
            full = amount == money(t["amount_paid"])
            t = conn.execute(
                """update pos_transactions set refund_amount = %s, refund_method = 'cash', refund_reason = %s,
                     refund_approved_by = %s, refunded_at = now(), status = case when %s then 'refunded' else status end
                   where id = %s returning *""",
                (amount, reason, session.get("staff"), full, t["id"]),
            ).fetchone()
            payload = sale_payload(conn, t)
    return jsonify(payload)


@app.get("/api/summary")
@require_unlock
def summary():
    rows = query(
        """select sale_day::text as day, sales, inspection_deposits, amount_paid::text, tax_total::text,
                  cash_received::text, change_given::text, coalesce(cash_refunded, 0)::text as cash_refunded,
                  net_cash_in_drawer::text
           from pos_daily_cash_summary where location_id = %s and currency = %s order by sale_day desc limit 14""",
        (LOCATION_ID, CURRENCY),
    )
    return jsonify(rows)


# ---------------------------------------------------------------------------
# Pages and health
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    response = send_from_directory(app.static_folder, "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    try:
        query("select 1 as ok", one=True)
        db = "ok"
    except Exception as exc:  # noqa: BLE001
        db = f"error: {type(exc).__name__}"
    return jsonify({"status": "ok" if db == "ok" else "degraded", "db": db, "accessCode": "set" if access_code() else "missing",
                    "time": datetime.now(timezone.utc).isoformat()}), (200 if db == "ok" else 503)


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(503)
def api_error(err):
    if request.path.startswith("/api/") or request.path == "/healthz":
        return jsonify({"error": err.description}), err.code
    return err


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
