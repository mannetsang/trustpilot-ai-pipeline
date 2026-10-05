"""One-time import of the Airtable "HR Manager" base into Supabase (shp-ats).

Two sources, same result:

  --csv-dir DIR   CSVs downloaded from the Airtable web UI (works even while
                  the workspace is over its monthly API quota). In each table
                  open a view that shows EVERY field and has NO filters, then
                  "Download CSV". Files are matched by name prefix:
                    Jobs*.csv, Candidates*.csv, Employee*.csv,
                    Company Office*.csv, Insurance Eligibility*.csv
  --api           Read the base over the Airtable API (needs quota; resets on
                  the 1st of the month). Token from env AIRTABLE_TOKEN or
                  Secret Manager `AIRTABLE_COMPANY_TOKEN`.

Re-runnable: each table's previously *imported* rows (airtable_id set) are
replaced; rows the pipeline or dashboard created in Supabase are kept. A
"Needs Review" placeholder job the new pipeline created is merged into the
imported job of the same title. Candidates whose Gmail message the new
pipeline already ingested are skipped. Re-running AFTER HR starts editing in
the dashboard would overwrite their edits to imported rows, hence --yes.

Résumé attachments are copied into the `resumes` bucket (Airtable's file
links expire a few hours after export/fetch; the Drive link is kept either
way). --skip-resumes leaves them out.

Usage (from hr-ats/migrate, with the pipeline venv):
    python import_airtable.py --csv-dir ~/Downloads/hr-export --dry-run
    python import_airtable.py --csv-dir ~/Downloads/hr-export --yes
    python import_airtable.py --api --yes
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import ats_db as db  # noqa: E402

AIRTABLE_BASE = "appar5DLoak36lfyj"
LOCAL_TZ = ZoneInfo("America/Toronto")  # Airtable shows datetimes in local time

# kinds: text, date, datetime, number, int, list, attachment
TABLES = {
    "jobs": {
        "airtable_table": "tblEPFbViaY4EpjF8", "csv_prefix": "jobs",
        "fields": {
            "Job Title": ("title", "text"),
            "Status": ("status", "text"),
            "Date Posted": ("date_posted", "date"),
            "Employment Type": ("employment_type", "text"),
            "Work Arrangement": ("work_arrangement", "text"),
            "Department": ("department", "text"),
            "Company / Brand": ("company_brand", "text"),
            "Location": ("location", "text"),
            "Schedule": ("schedule", "text"),
            "Pay Min": ("pay_min", "number"),
            "Pay Max": ("pay_max", "number"),
            "Pay Period": ("pay_period", "text"),
            "Pay Currency": ("pay_currency", "text"),
            "Education Requirement": ("education_requirement", "text"),
            "Experience Required": ("experience_required", "text"),
            "Benefits": ("benefits", "list"),
            "Company Overview": ("company_overview", "text"),
            "Role Summary": ("role_summary", "text"),
            "Core Responsibilities": ("core_responsibilities", "text"),
            "Requirements & Qualifications": ("requirements_qualifications", "text"),
        },
    },
    "candidates": {
        "airtable_table": "tbl4I3BpES6LDla89", "csv_prefix": "candidates",
        "fields": {
            "Candidate Name": ("candidate_name", "text"),
            "Status": ("status", "text"),
            "Job Title Applied": ("job_title_applied", "text"),
            "Job Location": ("job_location", "text"),
            "Application Date": ("application_date", "date"),
            "Source": ("source", "text"),
            "Email": ("email", "text"),
            "Phone": ("phone", "text"),
            "Candidate Location": ("candidate_location", "text"),
            "Years of Experience": ("years_of_experience", "number"),
            "Key Skills": ("key_skills", "text"),
            "Relevant Experience": ("relevant_experience", "text"),
            "Education & Qualifications": ("education_qualifications", "text"),
            "AI Summary": ("ai_summary", "text"),
            "Résumé": ("resume", "attachment"),
            "Résumé Drive Link": ("resume_drive_link", "text"),
            "Indeed Profile Link": ("indeed_profile_link", "text"),
            "Gmail Message ID": ("gmail_message_id", "text"),
            "Match Score": ("match_score", "int"),
            "Match Notes": ("match_notes", "text"),
        },
    },
    "employees": {
        "airtable_table": "tblZ38T0qi31dW4jD", "csv_prefix": "employee",
        "fields": {
            "Candidate Name": ("candidate_name", "text"),
            "Email": ("email", "text"),
            "Job Title": ("job_title", "text"),
            "Compensation Type": ("compensation_type", "text"),
            "Compensation Amount": ("compensation_amount", "number"),
            "Location": ("location", "text"),
            "Employment Type": ("employment_type", "text"),
            "Start Date": ("start_date", "date"),
            "Probation Period (Months)": ("probation_period_months", "int"),
            "Offer Emailed At": ("offer_emailed_at", "datetime"),
            "Offer Packet Sent At": ("offer_packet_sent_at", "datetime"),
        },
    },
    "company_offices": {
        "airtable_table": "tblPK8Q0ZsxH1BEUP", "csv_prefix": "company office",
        "fields": {
            "Location": ("location", "text"),
            "Address": ("address", "text"),
        },
    },
    "insurance_eligibility": {
        "airtable_table": "tbl5VPJz3EHqI4L3Z", "csv_prefix": "insurance eligibility",
        "fields": {
            "From": ("from_label", "text"),
            "Eligible Date": ("eligible_date_label", "text"),
        },
    },
}
REQUIRED = {"jobs": "title", "company_offices": "location",
            "insurance_eligibility": "from_label"}


# ------------------------------------------------------------- sources
def airtable_token():
    tok = os.environ.get("AIRTABLE_TOKEN")
    if tok:
        return tok
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    name = "projects/shp-ai-bot-2026/secrets/AIRTABLE_COMPANY_TOKEN/versions/latest"
    return client.access_secret_version(name=name).payload.data.decode().strip()


def fetch_api(table_id):
    """Yield (airtable_id, created_time, fields) for every record."""
    headers = {"Authorization": f"Bearer {airtable_token()}"}
    offset = None
    while True:
        params = {"pageSize": 100, **({"offset": offset} if offset else {})}
        r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{table_id}",
                         headers=headers, params=params, timeout=30)
        if r.status_code == 429:
            sys.exit(f"Airtable API refused (429): {r.text[:300]}\n"
                     "Use --csv-dir with a web-UI export instead.")
        r.raise_for_status()
        data = r.json()
        for rec in data["records"]:
            yield rec["id"], rec.get("createdTime"), rec["fields"]
        offset = data.get("offset")
        if not offset:
            return


def find_csv(directory, prefix):
    matches = sorted(p for p in Path(directory).glob("*.csv")
                     if p.name.lower().startswith(prefix))
    if len(matches) > 1:
        sys.exit(f"Several CSVs start with '{prefix}': {[m.name for m in matches]}")
    return matches[0] if matches else None


def read_csv(path):
    """Yield (synthetic_id, None, fields) per row. CSVs carry no record ids, so
    the id is a hash of the row (only used to tell imported rows apart)."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            fields = {k.strip(): v for k, v in row.items()
                      if k and v is not None and v.strip() != ""}
            if not fields:
                continue
            digest = hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()
            yield f"csv:{digest[:20]}", None, fields


# ------------------------------------------------------------- parsing
NUM_RE = re.compile(r"[^0-9.\-]")
ATTACH_RE = re.compile(r"\s*(.+?)\s*\((https?://[^)\s]+)\)")


def day_first(values):
    """Airtable's 'Local' date format follows the exporter's browser locale.
    Treat a column as D/M/Y if any value's first part can't be a month."""
    for v in values:
        m = re.match(r"\s*(\d{1,2})/(\d{1,2})/\d{4}", v or "")
        if m and int(m.group(1)) > 12:
            return True
    return False


def parse_date(v, dayfirst):
    if not isinstance(v, str):
        return None
    v = v.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    fmts = ["%d/%m/%Y", "%m/%d/%Y"] if dayfirst else ["%m/%d/%Y", "%d/%m/%Y"]
    for fmt in fmts + ["%B %d, %Y", "%b %d, %Y", "%Y/%m/%d"]:
        try:
            return datetime.strptime(v.split(" ")[0] if "/" in v else v, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {v!r}")


def parse_datetime(v, dayfirst):
    if not isinstance(v, str):
        return None
    v = v.strip()
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=LOCAL_TZ)).isoformat()
    except ValueError:
        pass
    date_fmts = ["%d/%m/%Y", "%m/%d/%Y"] if dayfirst else ["%m/%d/%Y", "%d/%m/%Y"]
    for df in date_fmts + ["%Y-%m-%d"]:
        for tf in ["%I:%M%p", "%I:%M %p", "%H:%M"]:
            try:
                d = datetime.strptime(v.upper(), f"{df} {tf}")
                return d.replace(tzinfo=LOCAL_TZ).isoformat()
            except ValueError:
                continue
    return parse_date(v, dayfirst)  # date-only value


def parse_value(v, kind, dayfirst):
    if v is None:
        return None
    if kind == "text":
        if isinstance(v, (list, dict)):  # e.g. a lookup/select returned as object
            return json.dumps(v, ensure_ascii=False)
        return str(v).strip() or None
    if kind in ("number", "int"):
        if isinstance(v, (int, float)):
            n = v
        else:
            s = NUM_RE.sub("", str(v))
            if s in ("", "-", "."):
                return None
            n = float(s)
        return int(round(n)) if kind == "int" else n
    if kind == "date":
        return parse_date(v, dayfirst)
    if kind == "datetime":
        return parse_datetime(v, dayfirst)
    if kind == "list":
        if isinstance(v, list):
            return [str(x) for x in v]
        return [x.strip() for x in str(v).split(",") if x.strip()]
    if kind == "attachment":
        if isinstance(v, list):  # API
            return [(a.get("filename") or "resume.pdf", a["url"], a.get("type"))
                    for a in v if a.get("url")]
        return [(name, url, None) for name, url in ATTACH_RE.findall(str(v))]
    raise ValueError(kind)


def to_row(table, airtable_id, created, fields, dayfirst_cols):
    spec = TABLES[table]["fields"]
    row = {"airtable_id": airtable_id, "airtable_fields": fields}
    if created:
        row["created_at"] = created
    attachments = []
    for name, value in fields.items():
        if name not in spec:
            continue  # kept in airtable_fields
        col, kind = spec[name]
        parsed = parse_value(value, kind, name in dayfirst_cols)
        if kind == "attachment":
            attachments = parsed or []
        else:
            row[col] = parsed
    return row, attachments


# ------------------------------------------------------------- writing
def copy_resume(attachments):
    """Download the first attachment and store it; return (path, filename)."""
    for filename, url, ctype in attachments[:1]:
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"download {r.status_code} (link expired? re-export)")
        ctype = ctype or r.headers.get("content-type", "application/pdf").split(";")[0]
        return db.upload_resume(r.content, filename, ctype), filename
    return None, None


def copy_resumes(pairs):
    """Copy résumés in parallel: thousands at ~1 s each would outlast
    Airtable's file links."""
    from concurrent.futures import ThreadPoolExecutor

    def one(pair):
        row, attachments = pair
        try:
            row["resume_path"], row["resume_filename"] = copy_resume(attachments)
            return None
        except Exception as e:
            return f"{row.get('candidate_name')}: {e}"

    with ThreadPoolExecutor(max_workers=12) as pool:
        errors = [e for e in pool.map(one, pairs) if e]
    for e in errors[:5]:
        print(f"  ! résumé for {e}")
    print(f"  résumés copied: {len(pairs) - len(errors)}, failed: {len(errors)}")


def delete_imported(table):
    if table == "candidates":
        old = db.select("candidates", {"select": "resume_path",
                                       "airtable_id": "not.is.null",
                                       "resume_path": "not.is.null"})
        paths = [r["resume_path"] for r in old]
        for i in range(0, len(paths), 100):
            r = requests.delete(f"{db.SUPABASE_URL}/storage/v1/object/{db.RESUME_BUCKET}",
                                headers=db._headers(), json={"prefixes": paths[i:i + 100]},
                                timeout=60)
            db._check(r, "resume cleanup")
    r = requests.delete(f"{db.SUPABASE_URL}/rest/v1/{table}",
                        params={"airtable_id": "not.is.null"}, headers=db._headers(),
                        timeout=60)
    db._check(r, f"clear imported {table}")


def insert_many(table, rows):
    for i in range(0, len(rows), 200):
        r = requests.post(f"{db.SUPABASE_URL}/rest/v1/{table}", headers=db._headers(),
                          json=rows[i:i + 200], timeout=60)
        db._check(r, f"insert {table}")


def import_table(table, records, args):
    records = list(records)
    spec = TABLES[table]["fields"]
    date_cols = [n for n, (_, k) in spec.items() if k in ("date", "datetime")]
    dayfirst_cols = {n for n in date_cols
                     if day_first([f.get(n) for _, _, f in records if isinstance(f.get(n), str)])}
    unknown = sorted({k for _, _, f in records for k in f} - set(spec))

    rows, errors = [], []
    for airtable_id, created, fields in records:
        try:
            rows.append(to_row(table, airtable_id, created, fields, dayfirst_cols))
        except ValueError as e:
            errors.append(f"{airtable_id}: {e}")
    req = REQUIRED.get(table)
    if req:
        missing = [r for r, _ in rows if not r.get(req)]
        rows = [(r, a) for r, a in rows if r.get(req)]
        if missing:
            errors.append(f"{len(missing)} row(s) without {req} skipped")

    print(f"\n{table}: {len(records)} source rows -> {len(rows)} to import"
          + (f"; day-first dates in {sorted(dayfirst_cols)}" if dayfirst_cols else ""))
    if unknown:
        print(f"  extra fields kept only in airtable_fields: {unknown}")
    for e in errors[:20]:
        print(f"  ! {e}")

    # Overlapping runs of the old pipeline recorded some emails more than once
    # and created the same placeholder job several times. Keep the earliest
    # (the one HR may have edited) and drop the rest.
    rows.sort(key=lambda ra: ra[0].get("created_at") or "")
    if table == "candidates":
        seen, kept = set(), []
        for r, a in rows:
            mid = r.get("gmail_message_id")
            if mid and mid in seen:
                continue
            seen.add(mid)
            kept.append((r, a))
        if len(kept) < len(rows):
            print(f"  dropped {len(rows) - len(kept)} duplicate candidate(s) "
                  "(same Gmail message recorded twice)")
        rows = kept
    if table == "jobs":
        titles, kept = set(), []
        for r, a in rows:
            auto = (r.get("status") == "Needs Review"
                    and (r.get("role_summary") or "").startswith("⚠ Auto-created"))
            if auto and r["title"] in titles:
                continue
            titles.add(r["title"])
            kept.append((r, a))
        # A placeholder that sorted before the real posting of the same title
        # is redundant too.
        real = {r["title"] for r, _ in kept if r.get("status") != "Needs Review"}
        kept = [(r, a) for r, a in kept
                if not (r.get("status") == "Needs Review" and r["title"] in real
                        and (r.get("role_summary") or "").startswith("⚠ Auto-created"))]
        if len(kept) < len(rows):
            print(f"  dropped {len(rows) - len(kept)} duplicate placeholder job(s)")
        rows = kept

    if args.dry_run:
        for r, a in rows[:2]:
            print("  sample:", {k: v for k, v in r.items() if k != "airtable_fields"},
                  f"+{len(a)} attachment(s)" if a else "")
        return

    already = db.select(table, {"select": "id", "airtable_id": "not.is.null", "limit": "1"})
    if already and not args.yes:
        sys.exit(f"{table} already has imported rows; re-run with --yes to replace them")
    delete_imported(table)

    if table == "jobs":
        placeholders = {r["title"]: r["id"] for r in db.select(
            "jobs", {"select": "id,title", "airtable_id": "is.null",
                     "status": "eq.Needs Review", "deleted_at": "is.null"})}
        remaining, merged = [], 0
        for row, attachments in rows:
            pid = placeholders.pop(row["title"], None)
            if pid:
                db.update("jobs", pid, row)
                merged += 1
            else:
                remaining.append((row, attachments))
        rows = remaining
        if merged:
            print(f"  merged {merged} pipeline placeholder job(s) into imported jobs")

    if table == "candidates":
        ids = [r["gmail_message_id"] for r, _ in rows if r.get("gmail_message_id")]
        have = set()
        for i in range(0, len(ids), 100):
            have |= {x["gmail_message_id"] for x in db.select(
                "candidates", {"select": "gmail_message_id",
                               "gmail_message_id": db.in_list(ids[i:i + 100])})}
        if have:
            print(f"  skipping {len(have)} already ingested by the new pipeline")
            rows = [(r, a) for r, a in rows if r.get("gmail_message_id") not in have]
        if not args.skip_resumes:
            copy_resumes([(r, a) for r, a in rows if a])

    # PostgREST bulk insert needs every object to have the same keys, and
    # created_at is NOT NULL: fill any gap rather than send null.
    keys = sorted({k for r, _ in rows for k in r})
    if "created_at" in keys:
        now = datetime.now(LOCAL_TZ).isoformat()
        for r, _ in rows:
            r.setdefault("created_at", now)
    insert_many(table, [{k: r.get(k) for k in keys} for r, _ in rows])
    print(f"  inserted {len(rows)}")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Import the Airtable HR Manager base into Supabase")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv-dir", help="folder of CSVs exported from the Airtable UI")
    src.add_argument("--api", action="store_true", help="read over the Airtable API")
    ap.add_argument("--tables", default=",".join(TABLES),
                    help="comma-separated subset (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    ap.add_argument("--yes", action="store_true", help="replace previously imported rows")
    ap.add_argument("--skip-resumes", action="store_true", help="don't copy résumé files")
    args = ap.parse_args()

    for table in args.tables.split(","):
        cfg = TABLES[table]
        if args.api:
            records = fetch_api(cfg["airtable_table"])
        else:
            path = find_csv(args.csv_dir, cfg["csv_prefix"])
            if not path:
                print(f"\n{table}: no {cfg['csv_prefix']}*.csv in {args.csv_dir}; skipped")
                continue
            records = read_csv(path)
        import_table(table, records, args)


if __name__ == "__main__":
    main()
