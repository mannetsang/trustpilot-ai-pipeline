"""Offer-packet automation for the Superhairpieces ATS.

When a candidate replies to their offer email ("... offer (quick detail needed)")
and includes their residential mailing address, this:
  1. Looks the candidate up in the Supabase `employees` table (by their
     email) — the offer details (comp, location, start date, etc.) were
     recorded there by the dashboard's "Draft hire email" flow.
  2. Creates a Drive folder named after the candidate under the onboarding parent.
  3. Fills the confidentiality agreement and the offer letter templates
     (Docs API replaceAllText on a converted copy — preserves formatting) and
     exports both to PDF into the folder.
  4. Emails the candidate the offer letter + confidentiality agreement + the two
     static forms (Employee Information Form, Video Surveillance Form), from the
     connected Gmail with its signature.
  5. Marks the reply with a Gmail label and stamps offer_packet_sent_at on the
     employee row so nobody is ever double-processed.

Usage:
    python offer_packet.py --run                 # process new replies (scheduled)
    python offer_packet.py --backfill            # ignore the processed label
    python offer_packet.py --run --dry-run       # detect + parse only, send nothing
    python offer_packet.py --test EMAIL --to X   # generate+send a packet for the
                                                 # Employee with EMAIL, to address X
                                                 # (with a placeholder mailing address)
"""
import argparse
import base64
import io
import os
import re
import sys
from datetime import date, datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload
from pydantic import BaseModel

import ats_db as db
import indeed_pipeline as pipe

LOCK_NAME = "offer-packet"
LOCK_TTL_SECONDS = 960  # > the 900 s task timeout

# employees columns -> the field names generate_and_send() reads.
EMPLOYEE_FIELD_NAMES = {
    "candidate_name": "Candidate Name",
    "email": "Email",
    "job_title": "Job Title",
    "compensation_type": "Compensation Type",
    "compensation_amount": "Compensation Amount",
    "location": "Location",
    "employment_type": "Employment Type",
    "start_date": "Start Date",
    "probation_period_months": "Probation Period (Months)",
    "offer_emailed_at": "Offer Emailed At",
    "offer_packet_sent_at": "Offer Packet Sent At",
}

# --- Google Drive / Docs ---
PARENT_FOLDER_ID = "1MkTcwdk47dA2BAzsD72Fy5eIuXqIFLwE"   # onboarding parent
STATIC_FORMS_FOLDER_ID = "1wFgDVaPePGFRQBGS9WQj29mpBeBA2OYI"  # static PDFs
CONF_TEMPLATE_ID = "1FnV9IvGPfpgwUW-4FQWN2HFW7nzP9R9O"    # Confidentiality agreement - Template.docx
OFFER_TEMPLATE_ID = "1Nswj2FU-YXWINrlPETqg-lE7-9WxueTL"   # Job offer - Template.docx
STATIC_FORM_NAMES = ["employee information form", "video surveillance form"]

# --- Gmail scope of work ---
OFFER_SUBJECT_QUERY = 'subject:"offer (quick detail needed)"'
# The packet is written to Gmail Drafts, never sent — HR reviews and sends it.
DRAFTED_LABEL = "Offer Packet Drafted"
NO_ADDRESS_LABEL = "Offer Reply - No Address"

WORKING_HOURS = "9:30am to 17:30pm or 10:00am to 18:00pm"

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
MAPS_API_KEY = os.environ.get("MAPS_API_KEY")
# A Canadian postal code (A1A 1A1) or US ZIP (12345 / 12345-6789).
POSTAL_RE = re.compile(r"[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d|\b\d{5}(?:-\d{4})?\b")


class AddressCheck(BaseModel):
    has_address: bool
    address: str | None = None


# ---------- auth ----------
def get_services():
    token_file = pipe.resolve_token_file()
    creds = Credentials.from_authorized_user_file(str(token_file), pipe.SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        try:
            token_file.write_text(creds.to_json(), encoding="utf-8")
        except OSError:
            pass  # read-only fs in cloud; refresh token is reused each run
    gmail = build("gmail", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    return gmail, drive, docs


# ---------- Employee table ----------
def get_employee_by_email(email):
    """Newest employee row for this email, as {"id", "fields"} (Airtable-shaped
    so the rest of this file reads it unchanged)."""
    # Case-insensitive exact match: escape LIKE's wildcards (_ is common in emails).
    pattern = email.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.select("employees", {
        "select": "id," + ",".join(EMPLOYEE_FIELD_NAMES),
        "email": f"ilike.{pattern}",
        "order": "created_at.desc",
        "limit": "1",
    })
    if not rows:
        return None
    row = rows[0]
    fields = {EMPLOYEE_FIELD_NAMES[k]: v for k, v in row.items()
              if k in EMPLOYEE_FIELD_NAMES and v is not None}
    return {"id": row["id"], "fields": fields}


def load_office_addresses():
    """Return {location name: full mailing address} from company_offices."""
    out = {}
    for row in db.select("company_offices", {"select": "location,address"}):
        loc = (row.get("location") or "").strip()
        addr = (row.get("address") or "").strip()
        if loc and addr:
            out[loc] = addr
    return out


MONTH_NUM = {
    m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}


def _parse_month_day(s):
    """'June 01' / 'January 21' -> (month_number, day) or None."""
    parts = (s or "").split()
    if len(parts) < 2:
        return None
    month = MONTH_NUM.get(parts[0].strip().capitalize())
    try:
        day = int(parts[1])
    except ValueError:
        return None
    return (month, day) if month else None


def load_insurance_mapping():
    """Return [(from_month, eligible_month, eligible_day)] from insurance_eligibility."""
    rows = []
    for row in db.select("insurance_eligibility",
                         {"select": "from_label,eligible_date_label"}):
        frm = _parse_month_day(row.get("from_label"))
        elig = _parse_month_day(row.get("eligible_date_label"))
        if frm and elig:
            rows.append((frm[0], elig[0], elig[1]))
    return rows


def compute_insurance_date(start_iso, rows):
    """Health-insurance eligibility date for a given job start date, per the
    Insurance Eligibility table. Ranges run the 21st -> the 20th; the eligible
    month rolls to the next year when it falls before the start month."""
    if not start_iso or not rows:
        return ""
    try:
        d = datetime.strptime(start_iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return ""
    from_month = d.month if d.day >= 21 else (d.month - 1 or 12)
    match = next((row for row in rows if row[0] == from_month), None)
    if not match:
        return ""
    _, elig_month, elig_day = match
    year = d.year if elig_month > d.month else d.year + 1
    return letter_date(date(year, elig_month, elig_day))


def mark_employee_sent(rec_id):
    db.update("employees", rec_id,
              {"offer_packet_sent_at": datetime.now(timezone.utc).isoformat()})


# ---------- formatting ----------
def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def letter_date(d):
    return f"{d:%B} {d.day}, {d.year}"


def agreement_date(d):
    return f"{_ordinal(d.day)} day of {d:%B}, {d.year}"


def format_start_date(iso):
    if not iso:
        return ""
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d")
        return letter_date(d)
    except ValueError:
        return iso


def format_compensation(fields):
    amt = fields.get("Compensation Amount")
    ctype = (fields.get("Compensation Type") or "").strip()
    if amt is None:
        return ""
    if ctype == "Hourly":
        return f"${amt:,.2f} per hour"
    if ctype == "Salary":
        return f"${amt:,.2f} per year"
    return f"${amt:,.2f}"


def complete_address(address):
    """If the candidate's address has no postal/ZIP code, look one up via Google
    Maps Geocoding and append it (keeping their street/city as written). Returns
    the address unchanged on any failure or if it already has one."""
    if not address or POSTAL_RE.search(address) or not MAPS_API_KEY:
        return address
    try:
        r = requests.get(
            GEOCODE_URL, params={"address": address, "key": MAPS_API_KEY}, timeout=15
        )
        data = r.json()
        if data.get("status") != "OK" or not data.get("results"):
            return address
        for comp in data["results"][0].get("address_components", []):
            if "postal_code" in comp.get("types", []):
                base = address.rstrip().rstrip(",").rstrip()
                return f"{base}, {comp['long_name']}"
    except Exception:
        return address
    return address


# ---------- document generation ----------
def fill_template_to_pdf(drive, docs, template_id, mapping, temp_name):
    """Copy a .docx template to a temp Google Doc, replace {{placeholders}} with
    the Docs API (formatting-preserving), export to PDF bytes, delete the temp."""
    copy = drive.files().copy(
        fileId=template_id,
        body={"name": temp_name, "mimeType": "application/vnd.google-apps.document"},
        supportsAllDrives=True,
    ).execute()
    doc_id = copy["id"]
    try:
        requests_body = [
            {"replaceAllText": {
                "containsText": {"text": k, "matchCase": True},
                "replaceText": v if v is not None else "",
            }}
            for k, v in mapping.items()
        ]
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests_body}).execute()

        req = drive.files().export_media(fileId=doc_id, mimeType="application/pdf")
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    finally:
        try:
            drive.files().delete(fileId=doc_id, supportsAllDrives=True).execute()
        except Exception:
            pass


def create_candidate_folder(drive, name):
    folder_name = pipe.sanitize(name)
    # Reuse an existing folder of the same name under the parent (idempotent on retries).
    esc = folder_name.replace("'", "\\'")
    existing = drive.files().list(
        q=(f"'{PARENT_FOLDER_ID}' in parents and name = '{esc}' "
           "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"),
        fields="files(id,webViewLink)", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if existing:
        return existing[0]["id"], existing[0].get("webViewLink")
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [PARENT_FOLDER_ID],
    }
    f = drive.files().create(body=meta, fields="id,webViewLink", supportsAllDrives=True).execute()
    return f["id"], f.get("webViewLink")


def save_pdf(drive, pdf_bytes, filename, folder_id):
    media = MediaInMemoryUpload(pdf_bytes, mimetype="application/pdf", resumable=False)
    f = drive.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    return f["id"]


def fetch_static_forms(drive):
    """Return [(filename, bytes)] for the static onboarding PDFs, matched by name."""
    resp = drive.files().list(
        q=f"'{STATIC_FORMS_FOLDER_ID}' in parents and trashed = false",
        fields="files(id,name)", supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    out = []
    for want in STATIC_FORM_NAMES:
        match = next((f for f in files if want in f["name"].lower()), None)
        if not match:
            raise RuntimeError(f"Static form '{want}' not found in folder {STATIC_FORMS_FOLDER_ID}")
        req = drive.files().get_media(fileId=match["id"])
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        out.append((match["name"], buf.getvalue()))
    return out


# ---------- Gmail signature + send ----------
def get_signature(gmail):
    try:
        resp = gmail.users().settings().sendAs().list(userId="me").execute()
        sends = resp.get("sendAs", [])
        primary = next((s for s in sends if s.get("isPrimary")), sends[0] if sends else None)
        return (primary or {}).get("signature", "") or ""
    except Exception:
        return ""


def draft_packet(gmail, to_email, subject, html_body, attachments):
    """Create the packet as a Gmail DRAFT (never sent). HR reviews and sends it."""
    msg = MIMEMultipart("mixed")
    msg["To"] = to_email
    msg["Subject"] = subject
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)
    for filename, data in attachments:
        part = MIMEApplication(data, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


def build_email_html(first_name, start_date, location, signature):
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    fn, sd, loc = esc(first_name), esc(start_date), esc(location)
    body = (
        '<div dir="ltr">'
        f"<p>Dear {fn},</p>"
        "<p>We are thrilled to welcome you to the Superhairpieces team! "
        "Congratulations on your new role with us.</p>"
        "<p>Attached to this email, you will find your official job offer outlining "
        "the details of your position.</p>"
        "<p><b>First Day Information</b></p>"
        "<ul>"
        f"<li>Start Date: {sd}</li>"
        "<li>Arrival Time: 10:30 AM (First day only)</li>"
        f"<li>Location: {loc}</li>"
        "</ul>"
        "<p><b>Pre-Onboarding Checklist</b></p>"
        "<p>To ensure a smooth onboarding process, please send the following documents "
        'to <a href="mailto:hr@superhairpieces.com">hr@superhairpieces.com</a> prior to '
        "your start date:</p>"
        '<ul style="list-style:none;padding-left:0;">'
        "<li>&#9744; Direct Deposit Information: Official document from your bank</li>"
        "<li>&#9744; Two Forms of ID (e.g., Passport, Health Card, Driver&#39;s License)</li>"
        "<li>&#9744; Proof of Work Eligibility: Work Permit, PR Card, or Passport (if Citizen)</li>"
        "<li>&#9744; Social Insurance Number (SIN)</li>"
        "<li>&#9744; Educational Certificate: High school, university, or course diploma</li>"
        "<li>&#9744; Employee Information Form (Attached &ndash; includes personal &amp; emergency contact details)</li>"
        "<li>&#9744; Video Surveillance Form (Attached)</li>"
        "</ul>"
        "<p><b>Action Required: Confidentiality Agreement &amp; Handbook</b></p>"
        "<ol>"
        "<li>Confidentiality Agreement: Please sign this electronically before your start "
        'date via the Adobe E-Signature system here: '
        '<a href="https://acrobat.adobe.com/link/home/">Adobe E-Sign Portal</a>.</li>'
        "<li>Employee Handbook: Please review the "
        '<a href="https://www.canva.com/design/DAG1JeZMYhA/9LeKYPQJ0tha0I5RRTMaXA/view'
        "?utm_content=DAG1JeZMYhA&amp;utm_campaign=designshare&amp;utm_medium=link2"
        '&amp;utm_source=uniquelinks&amp;utlId=h7094a2099c">Employee Handbook</a> '
        "regarding company rules and regulations.</li>"
        "</ol>"
        "<p>If you have any questions or need further information before your start date, "
        "please don&#39;t hesitate to reach out. We are excited to have you join our team "
        "and look forward to working together!</p>"
    )
    if signature:
        body += f"<br>{signature}"
    body += "</div>"
    return body


def generate_and_send(gmail, drive, docs, employee_fields, address, to_override=None):
    """Create folder, fill both docs -> PDF, save, and email the packet.
    Returns (folder_link, message_id)."""
    name = (employee_fields.get("Candidate Name") or "Candidate").strip()
    first_name = name.split()[0] if name else "there"
    job_title = (employee_fields.get("Job Title") or "").strip()
    recipient = to_override or employee_fields.get("Email")
    address = complete_address(address)  # append postal code if missing
    today = datetime.now(timezone.utc)

    # Work location on the letter = the office's full address (fall back to the
    # bare location name if that office isn't mapped in Company Office yet).
    location = (employee_fields.get("Location") or "").strip()
    work_location = load_office_addresses().get(location, location)
    start_date = format_start_date(employee_fields.get("Start Date"))
    insurance_date = compute_insurance_date(
        employee_fields.get("Start Date"), load_insurance_mapping())

    offer_map = {
        "{{DATE}}": letter_date(today),
        "{{CANDIDATE_NAME}}": name,
        "{{ADDRESS}}": address,
        "{{JOB_TITLE}}": job_title,
        "{{WORKING_HOURS}}": WORKING_HOURS,
        "{{START_DATE}}": start_date,
        "{{WORK_LOCATION}}": work_location,
        "{{COMPENSATION}}": format_compensation(employee_fields),
        "{{insurance starting date}}": insurance_date,
    }
    # Offer letter probation clause: "a {{number}} month probationary period".
    # Fill with the chosen duration (default 3 months if not recorded).
    months = employee_fields.get("Probation Period (Months)")
    offer_map["{{number}}"] = str(int(months)) if months else "3"
    conf_map = {
        "{{DATE}}": agreement_date(today),
        "{{CANDIDATE_NAME}}": name,
        "{{ADDRESS}}": address,
        "{{JOB_TITLE}}": job_title,
    }

    folder_id, folder_link = create_candidate_folder(drive, name)
    offer_pdf = fill_template_to_pdf(drive, docs, OFFER_TEMPLATE_ID, offer_map,
                                     f"tmp-offer-{name}")
    conf_pdf = fill_template_to_pdf(drive, docs, CONF_TEMPLATE_ID, conf_map,
                                    f"tmp-conf-{name}")
    offer_fn = pipe.sanitize(f"Offer Letter - {name}") + ".pdf"
    conf_fn = pipe.sanitize(f"Confidentiality Agreement - {name}") + ".pdf"
    save_pdf(drive, offer_pdf, offer_fn, folder_id)
    save_pdf(drive, conf_pdf, conf_fn, folder_id)

    statics = fetch_static_forms(drive)
    attachments = [(offer_fn, offer_pdf), (conf_fn, conf_pdf)] + statics

    signature = get_signature(gmail)
    subject = "Welcome to Superhairpieces — your onboarding documents"
    html = build_email_html(first_name, start_date, work_location, signature)
    draft = draft_packet(gmail, recipient, subject, html, attachments)
    return folder_link, draft.get("id")


# ---------- address detection ----------
def check_address(body_text):
    prompt = (
        "The text below is a candidate's email reply to a job offer. Decide whether "
        "it contains the candidate's home/mailing address. Treat an address as "
        "present if there is at least a street address (a street number and name) "
        "and a city or town; a province/state and postal/zip code are helpful but "
        "NOT required. International formats are valid. If present, extract just the "
        "candidate's own address as a single tidy line suitable for a letter — "
        "ignore any quoted text from earlier emails in the thread. If there is no "
        "street-level address, set has_address to false. Do not invent anything.\n\n"
        "=== EMAIL REPLY ===\n" + body_text
    )
    result = pipe._generate([prompt], AddressCheck)
    return result or AddressCheck(has_address=False)


# ---------- main pipeline ----------
def sender_email(from_header):
    m = re.search(r"<([^>]+)>", from_header or "")
    return (m.group(1) if m else (from_header or "")).strip().lower()


def process(backfill=False, limit=None, dry_run=False):
    gmail, drive, docs = get_services()

    # Resolve label ids and filter by each message's labelIds directly. Gmail's
    # `label:` search does not reliably match label names containing spaces, so
    # name-based exclusion silently fails — filtering on ids is dependable.
    drafted_label = pipe.ensure_label(gmail, DRAFTED_LABEL)
    noaddr_label = pipe.ensure_label(gmail, NO_ADDRESS_LABEL)

    resp = gmail.users().messages().list(
        userId="me", q=f"{OFFER_SUBJECT_QUERY} -from:me", maxResults=100
    ).execute()
    msg_ids = [m["id"] for m in resp.get("messages", [])]
    print(f"Found {len(msg_ids)} candidate reply message(s) in scope.")

    processed = skipped = failed = 0
    for mid in msg_ids:
        if limit and processed >= limit:
            break
        full = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
        if not backfill and (
            drafted_label in full.get("labelIds", [])
            or noaddr_label in full.get("labelIds", [])
        ):
            continue  # already drafted or already checked (no address)
        payload = full["payload"]
        frm = pipe.header(payload, "From") or ""
        email = sender_email(frm)
        body = pipe.find_plain_text(payload)

        emp = get_employee_by_email(email)
        if not emp:
            print(f"  skip {email}: no Employee record (not an offer recipient)")
            skipped += 1
            continue
        if emp["fields"].get("Offer Packet Sent At"):
            print(f"  skip {email}: packet already drafted")
            if not dry_run:
                gmail.users().messages().modify(
                    userId="me", id=mid, body={"addLabelIds": [drafted_label]}).execute()
            skipped += 1
            continue

        try:
            ac = check_address(body)
        except Exception as e:
            print(f"  ! address check failed for {email}: {e}")
            failed += 1
            continue

        if not ac.has_address:
            print(f"  {email}: reply has no address yet — marking checked")
            if not dry_run:
                gmail.users().messages().modify(
                    userId="me", id=mid, body={"addLabelIds": [noaddr_label]}).execute()
            skipped += 1
            continue

        name = emp["fields"].get("Candidate Name", email)
        if dry_run:
            print(f"  [dry-run] would draft packet for {name} <{email}>; address='{ac.address}'")
            processed += 1
            continue

        try:
            folder_link, draft_id = generate_and_send(gmail, drive, docs, emp["fields"], ac.address)
            mark_employee_sent(emp["id"])
            gmail.users().messages().modify(
                userId="me", id=mid, body={"addLabelIds": [drafted_label]}).execute()
            print(f"  OK packet DRAFTED for {name} <{email}> (draft {draft_id}); folder {folder_link}")
            processed += 1
        except Exception as e:
            print(f"  FAILED for {email}: {e}")
            failed += 1

    print(f"\nDone. processed={processed} skipped={skipped} failed={failed}")


def run_test(employee_email, to_addr):
    gmail, drive, docs = get_services()
    emp = get_employee_by_email(employee_email)
    if not emp:
        sys.exit(f"No Employee record for {employee_email}")
    address = "123 Test Street, Mississauga, ON L5N 5Z4"
    folder_link, draft_id = generate_and_send(
        gmail, drive, docs, emp["fields"], address, to_override=to_addr)
    print(f"TEST packet for {employee_email} DRAFTED to {to_addr} (draft {draft_id})")
    print(f"Folder: {folder_link}")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Offer-packet automation")
    ap.add_argument("--run", action="store_true", help="process new (unlabelled) replies")
    ap.add_argument("--backfill", action="store_true", help="ignore the processed labels")
    ap.add_argument("--dry-run", action="store_true", help="detect + parse only; send nothing")
    ap.add_argument("--limit", type=int, help="max packets to send")
    ap.add_argument("--test", metavar="EMPLOYEE_EMAIL", help="generate+send a packet for this Employee")
    ap.add_argument("--to", help="recipient override for --test")
    args = ap.parse_args()

    if args.test:
        run_test(args.test, args.to or args.test)
    elif args.dry_run:
        process(backfill=args.backfill, limit=args.limit, dry_run=True)
    else:
        with db.JobLock(LOCK_NAME, LOCK_TTL_SECONDS) as got:
            if not got:
                print("Another run holds the lock; exiting.")
                return
            process(backfill=args.backfill, limit=args.limit)


if __name__ == "__main__":
    main()
