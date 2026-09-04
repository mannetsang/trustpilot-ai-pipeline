"""Superhairpieces POS: the cash register for the ESI Montreal booth.

Serves the register page (static/), the admin portal (/admin) and a JSON API
over the Supabase Postgres database. Cash sales only. Every sale is written as
one pos_transactions row plus its pos_transaction_items, with the totals
checked again on the server so the browser can never ring a wrong amount.

Accounts live in pos_users: each person has a name and a personal PIN
(salted hash). Cashiers unlock the register; admins also open the portal and
approve refunds. The master code (below) opens the portal on its own, which is
how the first admin gets created and how access is recovered.

Env vars:
  SUPABASE_DB_URL     Postgres connection string. If unset, read from Secret
                      Manager (secret SUPABASE_DB_URL) with the runtime identity.
  POS_ACCESS_CODE     Master code for the admin portal. If unset, read from
                      Secret Manager (secret POS_ACCESS_CODE) and re-read every
                      minute, so it can be created or rotated without a deploy.
  POS_LOCATION_ID     Written on every sale. Default esi-montreal-2026.
  POS_LOCATION_NAME   Shown in the header. Default ESI Montreal 2026.
  POS_CURRENCY        Default CAD.
  GCP_PROJECT         Secret Manager project. Default shp-ai-bot-2026.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import secrets as pysecrets
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

import psycopg
from flask import Flask, Response, abort, jsonify, request, send_from_directory, session
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

app = Flask(__name__, static_folder="static", static_url_path="/static")

PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
LOCATION_ID = os.environ.get("POS_LOCATION_ID", "esi-montreal-2026")
LOCATION_NAME = os.environ.get("POS_LOCATION_NAME", "ESI Montreal 2026")
CURRENCY = os.environ.get("POS_CURRENCY", "CAD")
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
# Auth: personal PINs from pos_users; the master code opens the admin portal
# ---------------------------------------------------------------------------

PIN_RE = re.compile(r"^\d{4,8}$")


def hash_pin(pin, salt=None):
    salt = salt or pysecrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def pin_ok(pin, stored):
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_pin(pin, salt), stored)


def master_ok(candidate):
    expected = access_code()
    return bool(expected) and hmac.compare_digest(str(candidate).strip(), expected)


# Wrong-PIN throttle, per name, per instance: five misses lock a name for a minute.
_attempts = {}


def throttled(key):
    count, until = _attempts.get(key, (0, 0))
    return count >= 5 and until > time.time()


def record_attempt(key, ok):
    if ok:
        _attempts.pop(key, None)
        return
    count, _ = _attempts.get(key, (0, 0))
    _attempts[key] = (count + 1, time.time() + 60)


def clean_name(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:60]


def find_user(name):
    return query("select * from pos_users where lower(name) = lower(%s) and is_active", (name,), one=True)


def authenticate(name, pin):
    """Return the active user whose name and PIN match, or None. Throttles misses."""
    name = clean_name(name)
    key = name.lower()
    if not name or throttled(key):
        time.sleep(0.5)
        return None
    user = find_user(name)
    ok = bool(user) and pin_ok(str(pin or ""), user["pin_hash"])
    record_attempt(key, ok)
    if not ok:
        time.sleep(0.5)
        return None
    query("update pos_users set last_login_at = now() where id = %s returning id", (user["id"],))
    return user


def unlocked():
    return bool(session.get("user_id"))


def is_admin():
    return session.get("role") == "admin"


def require_unlock(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not unlocked():
            abort(401, "till is locked")
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin():
            abort(401, "admin sign-in required")
        return fn(*args, **kwargs)
    return wrapper


def approved_by_admin(body):
    """Refunds and other sensitive actions: an admin's name + PIN, or the master code."""
    if master_ok(body.get("code", "")):
        return "master code"
    user = authenticate(body.get("adminName"), body.get("adminPin"))
    if user and user["role"] == "admin":
        return user["name"]
    return None


@app.get("/api/config")
def config():
    return jsonify({
        "locationId": LOCATION_ID, "locationName": LOCATION_NAME, "currency": CURRENCY,
        "unlocked": unlocked(), "staff": session.get("staff"), "role": session.get("role"),
        "configured": access_code() is not None,
        "hasUsers": query("select exists (select 1 from pos_users where is_active) as e", one=True)["e"],
    })


@app.get("/api/users")
def user_names():
    """Active names for the lock screen. Public by design: names only."""
    rows = query("select name, role from pos_users where is_active order by role desc, lower(name)")
    return jsonify(rows)


@app.post("/api/unlock")
def unlock():
    body = request.get_json(silent=True) or {}
    user = authenticate(body.get("staff"), body.get("pin"))
    if not user:
        abort(403, "name or PIN wrong")
    session.permanent = True
    session["user_id"] = str(user["id"])
    session["staff"] = user["name"]
    session["role"] = user["role"]
    return jsonify({"ok": True, "staff": user["name"], "role": user["role"]})


@app.post("/api/lock")
def lock():
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin portal
# ---------------------------------------------------------------------------


def serialise_user(u):
    return {"id": str(u["id"]), "name": u["name"], "role": u["role"], "isActive": u["is_active"],
            "lastLoginAt": u["last_login_at"].isoformat() if u["last_login_at"] else None,
            "createdAt": u["created_at"].isoformat(), "createdBy": u["created_by"]}


@app.post("/api/admin/login")
def admin_login():
    body = request.get_json(silent=True) or {}
    if body.get("code"):
        if access_code() is None:
            abort(503, "master code not configured yet")
        if not master_ok(body["code"]):
            time.sleep(0.5)
            abort(403, "master code wrong")
        session.permanent = True
        session.update(user_id="master", staff="Master code", role="admin")
        return jsonify({"ok": True, "staff": "Master code", "role": "admin"})
    user = authenticate(body.get("staff"), body.get("pin"))
    if not user or user["role"] != "admin":
        abort(403, "name or PIN wrong, or not an admin")
    session.permanent = True
    session.update(user_id=str(user["id"]), staff=user["name"], role=user["role"])
    return jsonify({"ok": True, "staff": user["name"], "role": user["role"]})


@app.get("/api/admin/users")
@require_admin
def admin_users():
    rows = query("select * from pos_users order by is_active desc, role desc, lower(name)")
    return jsonify([serialise_user(u) for u in rows])


@app.post("/api/admin/users")
@require_admin
def admin_create_user():
    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name"))
    role = body.get("role", "cashier")
    pin = str(body.get("pin") or "")
    if not name:
        abort(400, "name required")
    if role not in ("cashier", "admin"):
        abort(400, "role must be cashier or admin")
    if not PIN_RE.match(pin):
        abort(400, "PIN must be 4 to 8 digits")
    if find_user(name):
        abort(409, "an active user with that name already exists")
    user = query(
        "insert into pos_users (name, role, pin_hash, created_by) values (%s, %s, %s, %s) returning *",
        (name, role, hash_pin(pin), session.get("staff")), one=True,
    )
    return jsonify(serialise_user(user)), 201


@app.patch("/api/admin/users/<uuid:user_id>")
@require_admin
def admin_update_user(user_id):
    body = request.get_json(silent=True) or {}
    user = query("select * from pos_users where id = %s", (str(user_id),), one=True)
    if not user:
        abort(404, "no such user")
    sets, params = [], []
    if "name" in body:
        name = clean_name(body["name"])
        if not name:
            abort(400, "name required")
        clash = find_user(name)
        if clash and str(clash["id"]) != str(user_id):
            abort(409, "an active user with that name already exists")
        sets.append("name = %s"); params.append(name)
    if "role" in body:
        if body["role"] not in ("cashier", "admin"):
            abort(400, "role must be cashier or admin")
        sets.append("role = %s"); params.append(body["role"])
    if "pin" in body:
        if not PIN_RE.match(str(body["pin"])):
            abort(400, "PIN must be 4 to 8 digits")
        sets.append("pin_hash = %s"); params.append(hash_pin(str(body["pin"])))
    if "isActive" in body:
        if body["isActive"] and find_user(user["name"]) and str(find_user(user["name"])["id"]) != str(user_id):
            abort(409, "an active user with that name already exists")
        sets.append("is_active = %s"); params.append(bool(body["isActive"]))
    if not sets:
        abort(400, "nothing to change")
    params.append(str(user_id))
    user = query(f"update pos_users set {', '.join(sets)} where id = %s returning *", params, one=True)
    if str(user_id) == session.get("user_id") and (not user["is_active"] or user["role"] != "admin"):
        session.clear()
    return jsonify(serialise_user(user))


# ---------------------------------------------------------------------------
# Admin: products by CSV
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["sku", "name", "category", "price", "cost", "quantity", "barcodes", "is_set", "is_clearance", "is_active", "note"]
CSV_HELP = {
    "sku": "required, unique; matches an existing product to update it",
    "name": "shown on the register; defaults to the SKU",
    "category": "e.g. CN, KR, US, Clearance",
    "price": "selling price, e.g. 8 or 8.00; blank makes the product unsellable",
    "cost": "optional",
    "quantity": "quantity brought to the show, whole number",
    "barcodes": "one or more scan codes separated by |, e.g. 3331684142074|X004MQWD9P",
    "is_set": "TRUE or FALSE",
    "is_clearance": "TRUE or FALSE",
    "is_active": "TRUE or FALSE; FALSE hides the product from the register",
    "note": "optional",
}
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 5000


def csv_response(rows, filename):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    # A BOM makes Excel open UTF-8 (accents, Chinese) correctly.
    body = "\ufeff" + out.getvalue()
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@app.get("/api/admin/products/template.csv")
@require_admin
def products_template():
    rows = [CSV_COLUMNS,
            ["EXAMPLE-SKU-1", "Example shampoo 350ml", "KR", "16.00", "7.50", "20", "8809640735561", "FALSE", "FALSE", "TRUE", ""],
            ["EXAMPLE-SKU-2", "Example brush set", "CN", "8", "3.10", "12", "3331684122427|3331684122434", "TRUE", "FALSE", "TRUE", "two barcodes"],
            ["# delete the two example rows above; column meanings: " + "; ".join(f"{k} = {v}" for k, v in CSV_HELP.items())]]
    return csv_response(rows, "pos-products-template.csv")


@app.get("/api/admin/products/export.csv")
@require_admin
def products_export():
    rows = query(
        """select p.sku, p.name, p.category, p.price, p.cost, p.show_qty, p.is_set, p.is_clearance, p.is_active, p.note,
                  coalesce((select string_agg(b.barcode, '|' order by b.barcode) from pos_product_barcodes b where b.product_id = p.id), '') as barcodes
           from pos_products p order by p.category, p.name"""
    )
    def flag(v):
        return "TRUE" if v else "FALSE"
    def num(v):
        return "" if v is None else str(v)
    body = [CSV_COLUMNS] + [[r["sku"], r["name"], r["category"], num(r["price"]), num(r["cost"]), num(r["show_qty"]),
                            r["barcodes"], flag(r["is_set"]), flag(r["is_clearance"]), flag(r["is_active"]), r["note"] or ""]
                           for r in rows]
    return csv_response(body, f"pos-products-{datetime.now(timezone.utc):%Y%m%d}.csv")


def parse_money_cell(text):
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    m = re.fullmatch(r"(?:CA\$|US\$|€|\$)?(-?\d+(?:\.\d+)?)", text)
    if not m:
        raise ValueError(f"not a price: {text!r}")
    value = Decimal(m.group(1))
    if value < 0:
        raise ValueError(f"negative: {text!r}")
    return money(value)


def parse_flag(text):
    t = (text or "").strip().lower()
    if t in ("", None):
        return None
    if t in ("true", "yes", "y", "1", "x"):
        return True
    if t in ("false", "no", "n", "0"):
        return False
    raise ValueError(f"not TRUE/FALSE: {text!r}")


def parse_products_csv(raw):
    """Return (rows, problems).

    A blank cell means "leave unchanged" for an existing product and "use the
    default" for a new one, so a file with just sku and price can fix prices
    without touching anything else. Only non-blank cells make it into a record.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = None
    rows, problems, seen = [], [], {}
    for n, cells in enumerate(reader, start=1):
        if not any(c.strip() for c in cells) or cells[0].strip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower().lstrip("\ufeff") for c in cells]
            unknown = [h for h in header if h and h not in CSV_COLUMNS]
            if "sku" not in header:
                raise ValueError("the header row must include a 'sku' column")
            if unknown:
                problems.append(f"ignored unknown column(s): {', '.join(unknown)}")
            continue
        if len(rows) >= MAX_CSV_ROWS:
            problems.append(f"stopped after {MAX_CSV_ROWS} rows")
            break
        cell = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header) if h}
        sku = cell.get("sku", "")
        blank_price = "price" in cell and not cell["price"]
        cell = {k: v for k, v in cell.items() if v}
        if not sku:
            problems.append(f"row {n}: blank sku, skipped")
            continue
        if sku.upper().startswith("EXAMPLE-SKU-"):
            problems.append(f"row {n}: example row skipped")
            continue
        if sku in seen:
            problems.append(f"row {n}: duplicate sku {sku!r}, first row wins")
            continue
        record = {"sku": sku, "row": n}
        try:
            if "name" in cell:
                record["name"] = cell["name"]
            if "category" in cell:
                record["category"] = cell["category"]
            if "price" in cell:
                record["price"] = parse_money_cell(cell["price"])
            record["blank_price"] = blank_price
            if "cost" in cell:
                record["cost"] = parse_money_cell(cell["cost"])
            if "quantity" in cell:
                q = cell["quantity"]
                if re.fullmatch(r"-?\d+", q):
                    record["show_qty"] = int(q)
                else:
                    problems.append(f"row {n} {sku}: quantity {q!r} is not a whole number, ignored")
            for key in ("is_set", "is_clearance", "is_active"):
                if key in cell:
                    v = parse_flag(cell[key])
                    if v is not None:
                        record[key] = v
            if "note" in cell:
                record["note"] = cell["note"]
            if "barcodes" in cell:
                record["barcodes"] = [b.strip() for b in re.split(r"[|;]", cell["barcodes"]) if b.strip()]
        except ValueError as exc:
            problems.append(f"row {n} {sku}: {exc}, row skipped")
            continue
        seen[sku] = n
        rows.append(record)
    if header is None:
        raise ValueError("the file is empty")
    return rows, problems


@app.post("/api/admin/products/import")
@require_admin
def products_import():
    upload = request.files.get("file")
    raw = upload.read(MAX_CSV_BYTES + 1) if upload else request.get_data(cache=False)[: MAX_CSV_BYTES + 1]
    if not raw:
        abort(400, "no file")
    if len(raw) > MAX_CSV_BYTES:
        abort(400, "file larger than 2 MB")
    preview = request.args.get("preview") == "1" or request.form.get("preview") == "1"
    try:
        rows, problems = parse_products_csv(raw)
    except ValueError as exc:
        abort(400, str(exc))

    skus = [r["sku"] for r in rows]
    existing = {r["sku"]: r for r in query("select * from pos_products where sku = any(%s)", (skus,))} if skus else {}
    to_add = [r for r in rows if r["sku"] not in existing]
    to_update = [r for r in rows if r["sku"] in existing]
    # A brand-new product with no price cannot be sold: say so up front.
    for r in to_add:
        if r.get("price") is None:
            problems.append(f"row {r['row']} {r['sku']}: new product without a price, imported as unsellable")
    unchanged = [r for r in to_update if not any(k in r for k in ("name", "category", "price", "cost", "show_qty", "is_set", "is_clearance", "is_active", "note", "barcodes"))]
    if unchanged:
        problems.append(f"{len(unchanged)} row(s) had only a sku and change nothing")
    summary = {"rows": len(rows), "added": len(to_add), "updated": len(to_update), "problems": problems, "preview": preview}
    if preview:
        return jsonify(summary)

    imported_at = datetime.now(timezone.utc)
    with pool().connection() as conn:
        with conn.transaction():
            for r in rows:
                fields = {k: v for k, v in r.items() if k in ("name", "category", "price", "cost", "show_qty", "is_set", "is_clearance", "is_active", "note")}
                is_new = r["sku"] not in existing
                if is_new:
                    fields.setdefault("name", r["sku"])
                    fields.setdefault("category", "Uncategorised")
                    if "price" not in fields:
                        fields["price"] = None
                # A product becomes sellable when it gets a price, unless is_active says otherwise.
                if "price" in fields and "is_active" not in fields:
                    fields["is_active"] = fields["price"] is not None
                fields["sheet"] = json.dumps({k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items() if k not in ("row", "blank_price")}, ensure_ascii=False)
                fields["sheet_row"] = r["row"]
                fields["imported_at"] = imported_at
                if not is_new:
                    sets = ", ".join(f"{k} = %s" for k in fields)
                    conn.execute(f"update pos_products set {sets} where sku = %s", [*fields.values(), r["sku"]])
                else:
                    cols = ["sku", *fields]
                    conn.execute(
                        f"insert into pos_products ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
                        [r["sku"], *fields.values()],
                    )
                if "barcodes" in r:
                    conn.execute("delete from pos_product_barcodes where product_id = (select id from pos_products where sku = %s)", (r["sku"],))
                    for code in r["barcodes"]:
                        conn.execute(
                            """insert into pos_product_barcodes (barcode, product_id)
                               select %s, id from pos_products where sku = %s
                               on conflict (barcode) do update set product_id = excluded.product_id""",
                            (code, r["sku"]),
                        )
            totals = conn.execute(
                "select (select count(*) from pos_products) as products, (select count(*) from pos_products where is_active and price is not null) as sellable,"
                " (select count(*) from pos_product_barcodes) as barcodes"
            ).fetchone()
    summary["database"] = totals
    return jsonify(summary)


@app.get("/admin")
def admin_page():
    response = send_from_directory(app.static_folder, "admin.html")
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

# Product photos come from the SkuVault-synced `products` table (pictures[]),
# matched on SKU. A scalar subquery keeps pos_products free of copied URLs and
# picks up new photos as soon as that table syncs.
IMAGE_SQL = ("(select pr.pictures[1] from products pr where pr.sku = p.sku"
             " and cardinality(pr.pictures) > 0 and pr.pictures[1] <> '' limit 1) as image_url")
PRODUCT_COLS = f"p.id, p.sku, p.name, p.category, p.price, p.is_set, p.is_clearance, p.show_qty, {IMAGE_SQL}"


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
        "image": r.get("image_url"),
    }


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def totals(lines, discount):
    """The shelf price is what the customer pays: subtotal minus discount, nothing added."""
    subtotal = sum((money(l["unit_price"]) * l["quantity"] for l in lines), Decimal("0"))
    discount = money(discount or 0)
    if discount < 0 or discount > subtotal:
        abort(400, "discount out of range")
    return subtotal, discount, money(subtotal - discount)


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

    subtotal, discount, grand = totals(lines, body.get("discount"))
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
            user_id = session.get("user_id")
            txn = conn.execute(
                """insert into pos_transactions (client_record_id, location_id, staff_name, staff_user_id, currency,
                     payment_method, subtotal, discount_total, grand_total, amount_paid, cash_received, change_given, note)
                   values (%s, %s, %s, %s, %s, 'cash', %s, %s, %s, %s, %s, %s, %s)
                   returning *""",
                (record_id, LOCATION_ID, session.get("staff") or "—", user_id if user_id != "master" else None, CURRENCY,
                 subtotal, discount, grand, grand, cash, change, note),
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
        "subtotal": s(t["subtotal"]), "discount": s(t["discount_total"]), "total": s(t["grand_total"]),
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
    # A refund needs an admin's approval: their name and PIN, or the master code.
    approver = session.get("staff") if is_admin() else approved_by_admin(body)
    if not approver:
        abort(403, "admin approval failed")
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
                (amount, reason, approver, full, t["id"]),
            ).fetchone()
            payload = sale_payload(conn, t)
    return jsonify(payload)


@app.get("/api/summary")
@require_unlock
def summary():
    rows = query(
        """select sale_day::text as day, sales, inspection_deposits, amount_paid::text,
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


@app.get("/api/health")
def health():
    # Not /healthz: Google's front end answers that path itself with a 404.
    try:
        query("select 1 as ok", one=True)
        db = "ok"
    except Exception as exc:  # noqa: BLE001
        db = f"error: {type(exc).__name__}"
    return jsonify({"status": "ok" if db == "ok" else "degraded", "db": db, "masterCode": "set" if access_code() else "missing",
                    "time": datetime.now(timezone.utc).isoformat()}), (200 if db == "ok" else 503)


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(503)
def api_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": err.description}), err.code
    return err


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
