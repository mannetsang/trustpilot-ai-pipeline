"""
Clover appointment emails -> TeamDesk Appointment records.

Clover's public API has no appointments endpoint and no appointment webhook,
so the booking confirmation email is the only source that carries the
appointment date and time. This service polls the mailbox on a schedule
(Cloud Scheduler hitting /poll every 5 minutes), and for every booking
confirmation from app@clover.com it:

  1. reads the salon name, date, time and receipt link from the email,
  2. opens the public receipt page for the service line items, price,
     customer and Clover order ID,
  3. creates a TeamDesk Appointment unless one with that POS ID (the Clover
     order ID) already exists.

The POS ID check is the only state: re-reading an email is harmless, so the
poll can look back a couple of days and survive missed runs and restarts.

Env vars (the secrets are mounted from Secret Manager by the setup workflow):
  EMAIL_USER           mailbox that receives the Clover emails
  GOOGLE_APP_PASSWORD  Google app password for that mailbox (IMAP)
  TEAMDESK_TOKEN       TeamDesk REST API token for database TEAMDESK_DB
  TEAMDESK_DB          (optional) default 56554
  TEAMDESK_TABLE       (optional) default t_504863 (Appointment)
  LOOKBACK_DAYS        (optional) default 2

The service is private (no unauthenticated access): Cloud Run only accepts
requests carrying a Google identity token for an account with run.invoker,
which the scheduler job sends via OIDC.

Endpoints:
  GET /poll                 create records for new bookings
  GET /poll?dry_run=1       parse and report, write nothing
  GET /poll?days=30         look further back (e.g. a backfill)
"""

import email
import html
import imaplib
import os
import re
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASSWORD = os.environ.get("GOOGLE_APP_PASSWORD", "")
TEAMDESK_TOKEN = os.environ.get("TEAMDESK_TOKEN", "")
TEAMDESK_DB = os.environ.get("TEAMDESK_DB", "56554")
TEAMDESK_TABLE = os.environ.get("TEAMDESK_TABLE", "t_504863")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))

TEAMDESK_API = f"https://www.teamdesk.net/secure/api/v2/{TEAMDESK_DB}/{TEAMDESK_TABLE}"
CLOVER_SENDER = "app@clover.com"
# The salon gets "An appointment was confirmed", a copy of the customer's
# "Appointment confirmed". Both match; a self-booking yields both, and the POS
# ID check keeps that to one record.
CLOVER_SUBJECT = re.compile(r"\bappointment\b.*\bconfirmed\b", re.I)
SOURCE = "CLOVER"

# Clover writes the zone as an abbreviation ("04:45 PM EDT").
TZ_OFFSETS = {
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
}


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------

def fetch_clover_emails(days):
    """Yield (message_id, html_body) for recent Clover booking confirmations."""
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        mail.login(EMAIL_USER, EMAIL_PASSWORD)
        mail.select('"[Gmail]/All Mail"', readonly=True)
        _, data = mail.search(
            None, "FROM", CLOVER_SENDER, "SUBJECT", "confirmed", "SINCE", since
        )
        for num in data[0].split():
            _, parts = mail.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(parts[0][1])
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            if not CLOVER_SUBJECT.search(subject):
                continue
            yield msg.get("Message-ID", num.decode()), _html_body(msg)
    finally:
        try:
            mail.logout()
        except Exception:  # noqa: BLE001 - already closing
            pass


def _html_body(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            charset = part.get_content_charset() or "utf-8"
            return part.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_email(body):
    """Pull salon, appointment time and receipt URL out of the confirmation."""
    text = _text(body)
    m = re.search(
        r"confirm your (.+?) appointment on (\d{2}/\d{2}/\d{4}) at (\d{1,2}:\d{2} [AP]M) ([A-Z]{3})",
        text,
    )
    receipt = re.search(r'href="(https://www\.clover\.com/r/[A-Z0-9]+)"', body)
    if not m or not receipt:
        return None
    salon, day, clock, zone = m.groups()
    local = datetime.strptime(f"{day} {clock}", "%m/%d/%Y %I:%M %p")
    offset = TZ_OFFSETS.get(zone)
    if offset is not None:
        local = local.replace(tzinfo=timezone(timedelta(hours=offset)))
    return {
        "salon": salon.strip(),
        "appointment_time": local.isoformat(),
        "receipt_url": receipt.group(1),
    }


# ---------------------------------------------------------------------------
# Receipt page (public, server-rendered)
# ---------------------------------------------------------------------------

def _span(page, cls):
    m = re.search(rf'<span class="{cls}">(.*?)</span>', page, re.S)
    return _text(m.group(1)) if m else ""


def parse_receipt(url):
    page = requests.get(url, timeout=30).text
    items = []
    for label in re.findall(r'<li class="line-item[^"]*"[^>]*>\s*<div class="label">(.*?)</div>', page, re.S):
        price = re.search(r'<span class="price">(.*?)</span>', label, re.S)
        name = _text(re.sub(r'<span class="price">.*?</span>', "", label, flags=re.S))
        if name:
            items.append({"name": name, "price": _text(price.group(1)) if price else ""})
    order = re.search(r"Order ID:\s*([A-Z0-9]+)", page)
    merchant = re.search(r"/v3/merchants/([A-Z0-9]+)/", page)
    total = re.search(r'Order total\s*<span class="price">(.*?)</span>', page, re.S)
    return {
        "order_id": order.group(1) if order else url.rsplit("/", 1)[-1],
        "merchant_id": merchant.group(1) if merchant else "",
        "items": items,
        "order_total": _text(total.group(1)) if total else "",
        "customer_name": _span(page, "customer-name"),
        "customer_email": _span(page, "customer-email"),
        "customer_phone": _span(page, "customer-phone"),
        "employee": _span(page, "cashier-info").removeprefix("Order Employee:").strip(),
    }


# ---------------------------------------------------------------------------
# TeamDesk
# ---------------------------------------------------------------------------

def _teamdesk(method, path, **kwargs):
    r = requests.request(
        method, f"{TEAMDESK_API}/{path}",
        headers={"Authorization": f"Bearer {TEAMDESK_TOKEN}"}, timeout=30, **kwargs,
    )
    r.raise_for_status()
    return r.json()


def appointment_exists(pos_id):
    rows = _teamdesk("GET", "select.json", params={
        "column": "Id", "filter": f"[POS ID]='{pos_id}'", "top": 1,
    })
    return bool(rows)


def build_record(booking, receipt):
    items = receipt["items"]
    # Hairstylist and client name are left for staff to fill in.
    notes = [f"Booked via Clover at {booking['salon']}."]
    if receipt["employee"]:
        notes.append(f"Booked by (Clover employee): {receipt['employee']}")
    record = {
        "Service Office": "USA",
        "Service Menu": ", ".join(i["name"] for i in items),
        "Service Price": receipt["order_total"] or ", ".join(i["price"] for i in items),
        "Appointment Time": booking["appointment_time"],
        "Appointment Status": "Open",
        "Manager Approve": "Open",
        "Incentive Record": "Not yet",
        "Model and Color": "N/A",
        "Source": SOURCE,
        "POS ID": receipt["order_id"],
        "Location ID": receipt["merchant_id"],
        "Receipt Link": booking["receipt_url"],
        "Internal Notes": "\n".join(notes),
    }
    if receipt["customer_email"]:
        record["Client Email"] = receipt["customer_email"]
    return record


def create_appointment(record):
    result = _teamdesk("POST", "create.json", json=[record])
    return result[0] if isinstance(result, list) and result else result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/poll", methods=["GET", "POST"])
def poll():
    dry_run = request.args.get("dry_run") in ("1", "true", "yes")
    days = int(request.args.get("days", LOOKBACK_DAYS))

    results = []
    for message_id, body in fetch_clover_emails(days):
        booking = parse_email(body)
        if not booking:
            results.append({"message": message_id, "status": "unparsed"})
            continue
        try:
            receipt = parse_receipt(booking["receipt_url"])
            record = build_record(booking, receipt)
            if appointment_exists(record["POS ID"]):
                results.append({"pos_id": record["POS ID"], "status": "exists"})
            elif dry_run:
                results.append({"pos_id": record["POS ID"], "status": "would_create", "record": record})
            else:
                created = create_appointment(record)
                print(f"Created TeamDesk appointment for Clover order {record['POS ID']}: {created}")
                results.append({"pos_id": record["POS ID"], "status": "created", "teamdesk": created})
        except Exception as exc:  # noqa: BLE001 - one bad email must not block the rest
            print(f"Failed on {booking['receipt_url']}: {exc}")
            results.append({"receipt": booking["receipt_url"], "status": "error", "error": str(exc)})

    return jsonify(dry_run=dry_run, days=days, results=results)


@app.route("/", methods=["GET"])
def health():
    return "ok"
