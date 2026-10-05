"""Indeed application pipeline for the Superhairpieces ATS.

For each new Indeed application email that carries a résumé PDF, this:
  1. Parses candidate name, job title, location, apply date, Indeed link
  2. Downloads the résumé PDF
  3. Uploads the PDF to the "Job Applications" Google Drive folder
  4. Uses Gemini to read the résumé and extract structured fields
  5. Scores the candidate against the job's requirements with Gemini
  6. Stores the résumé in Supabase Storage, creates a row in the Supabase
     `candidates` table (project shp-ats), and marks the email processed
     with a Gmail label

Cloud Scheduler fires this every minute, more often than a run lasts, so each
run takes a lease in `job_locks` first and exits quietly if another run holds
it. A message that fails 3 times is parked in `ingest_failures` and skipped.

Scope (what counts as an application email):
  sender ends with @indeedemail.com  AND
  subject starts with "[Action required] New application for"  AND
  a PDF attachment is present.
The bundled digests from employers-noreply@indeed.com (no résumé) and all
Indeed marketing mail are intentionally excluded.

Usage:
    python indeed_pipeline.py --backfill        # process all in-scope emails
    python indeed_pipeline.py --run             # process only un-labelled ones
    python indeed_pipeline.py --run --limit 5
    python indeed_pipeline.py --backfill --dry-run   # parse only, write nothing
"""
import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google import genai
from google.genai import types
from pydantic import BaseModel

import ats_db as db

BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / "token_pipeline.json"
ENV_FILE = BASE_DIR / ".env"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]

# A run's lease. Longer than the 900 s task timeout so a run can't lose it.
LOCK_NAME = "indeed-pipeline"
LOCK_TTL_SECONDS = 960
MAX_ATTEMPTS = 3  # then the message is parked in ingest_failures

# --- Google Drive ---
DRIVE_FOLDER_ID = "15K8HWc0kvXmhJPjBUp1KQW6Vk7oYU8CN"  # "Job Applications"

# --- Gemini via Vertex AI (uses the indeed-pipeline service account key) ---
GEMINI_MODEL = "gemini-2.5-flash"
VERTEX_PROJECT = "shp-ai-bot-2026"
VERTEX_LOCATION = "us-central1"
VERTEX_SA_FILE = BASE_DIR / "vertex-sa.json"

PROCESSED_LABEL = "Recorded-ATS"
SUBJECT_PREFIX = "new application for"  # matched case-insensitively

# When an application arrives for a job that isn't in the Jobs table yet, the
# pipeline creates a minimal placeholder Job so the candidate still groups onto
# the dashboard (which joins candidates to jobs by exact "Job Title"). The
# placeholder is marked with this status so HR can spot and complete it; it is
# deliberately NOT "Open", so it never inflates the open-positions count.
PLACEHOLDER_JOB_STATUS = "Needs Review"


def load_env():
    """Read .env into os.environ if present (local runs). Real env vars win."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()


def resolve_token_file():
    """Locate the Gmail/Drive OAuth token.

    Cloud: PIPELINE_OAUTH_JSON env var (from Secret Manager
    `hr-ats-pipeline-gmail-token`) holds the base64-encoded token JSON;
    decode it to a temp file. Local: use token_pipeline.json next to this file.
    """
    b64 = os.environ.get("PIPELINE_OAUTH_JSON")
    if b64:
        import tempfile
        path = Path(tempfile.gettempdir()) / "token_pipeline.json"
        path.write_bytes(base64.b64decode(b64))
        return path
    return TOKEN_FILE


# ---------- résumé extraction schema ----------
class ResumeData(BaseModel):
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    years_experience: float | None = None
    key_skills: str | None = None
    relevant_experience: str | None = None
    education_qualifications: str | None = None
    summary: str | None = None


class ScoreResult(BaseModel):
    score: int  # 0-100 fit score
    notes: str  # 1-2 sentences: key strengths and gaps


# ---------- auth ----------
def get_services():
    token_file = resolve_token_file()
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        try:
            token_file.write_text(creds.to_json(), encoding="utf-8")
        except OSError:
            pass  # read-only fs in cloud; the refresh token is reused each run
    gmail = build("gmail", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return gmail, drive


# ---------- Gmail helpers ----------
def header(payload, name):
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def find_pdf_attachment(payload):
    """Return (filename, attachmentId) of the first PDF part, else None."""
    fn = payload.get("filename", "")
    body = payload.get("body", {})
    if fn.lower().endswith(".pdf") and body.get("attachmentId"):
        return fn, body["attachmentId"]
    for p in payload.get("parts", []) or []:
        r = find_pdf_attachment(p)
        if r:
            return r
    return None


def find_plain_text(payload):
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for p in payload.get("parts", []) or []:
        r = find_plain_text(p)
        if r:
            return r
    return ""


def in_scope(payload):
    frm = (header(payload, "From") or "").lower()
    subj = (header(payload, "Subject") or "").lower()
    if "@indeedemail.com" not in frm:
        return False
    if SUBJECT_PREFIX not in subj:
        return False
    return find_pdf_attachment(payload) is not None


def parse_subject(subject):
    """'[Action required] New application for <Job>, <Location>' -> (job, location)."""
    m = re.search(r"new application for\s+(.*)", subject, re.I)
    rest = m.group(1).strip() if m else subject
    job, _, location = rest.partition(",")
    return job.strip(), location.strip()


def parse_body(text):
    """Extract candidate name and Indeed profile link from the plain-text body."""
    name = None
    m = re.search(r"^Name:\s*(.+)$", text, re.M)
    if m:
        name = m.group(1).strip()
    link = None
    m = re.search(r"https?://\S+", text)
    if m:
        link = m.group(0).rstrip(").,")
    return name, link


def ensure_label(gmail, name):
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    for l in labels:
        if l["name"] == name:
            return l["id"]
    created = gmail.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"},
    ).execute()
    return created["id"]


# ---------- Vertex AI (Gemini) ----------
def _vertex_client():
    # Local: use the service-account key file. Cloud: the Run job's own
    # identity (Application Default Credentials) — no key file needed.
    if VERTEX_SA_FILE.exists():
        sa_creds = service_account.Credentials.from_service_account_file(
            str(VERTEX_SA_FILE),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return genai.Client(vertexai=True, project=VERTEX_PROJECT,
                            location=VERTEX_LOCATION, credentials=sa_creds)
    return genai.Client(vertexai=True, project=VERTEX_PROJECT,
                        location=VERTEX_LOCATION)


def _generate(contents, schema):
    """Call Gemini with a structured-output schema, retrying transient 429s."""
    import time
    client = _vertex_client()
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema)
    last_err = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=cfg)
            return resp.parsed
        except Exception as e:
            last_err = e
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                time.sleep(2 * (attempt + 1))  # brief backoff on capacity errors
                continue
            raise
    raise last_err


def parse_resume(pdf_bytes):
    prompt = (
        "You are an HR assistant. Read this candidate's résumé and extract the "
        "requested fields. For years_experience, estimate total years of relevant "
        "professional experience as a number. For key_skills give a comma-separated "
        "list. For relevant_experience and education_qualifications, give concise "
        "plain-text summaries (a few lines each). Use null for anything not found. "
        "Do not invent information."
    )
    contents = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        prompt,
    ]
    return _generate(contents, ResumeData) or ResumeData()


def _job_text(job):
    """Flatten a Jobs record's fields into a requirements description."""
    parts = [
        ("Job Title", job.get("Job Title")),
        ("Role Summary", job.get("Role Summary")),
        ("Core Responsibilities", job.get("Core Responsibilities")),
        ("Requirements & Qualifications", job.get("Requirements & Qualifications")),
        ("Experience Required", job.get("Experience Required")),
        ("Education Requirement", job.get("Education Requirement")),
    ]
    return "\n".join(f"{k}: {v}" for k, v in parts if v)


def _candidate_text(cf):
    parts = [
        ("Candidate", cf.get("Candidate Name")),
        ("Years of Experience", cf.get("Years of Experience")),
        ("Key Skills", cf.get("Key Skills")),
        ("Relevant Experience", cf.get("Relevant Experience")),
        ("Education & Qualifications", cf.get("Education & Qualifications")),
        ("Summary", cf.get("AI Summary")),
    ]
    return "\n".join(f"{k}: {v}" for k, v in parts if v)


def score_candidate(candidate_fields, job_fields):
    """Return a ScoreResult (0-100 fit + notes) comparing candidate to job."""
    job_desc = _job_text(job_fields) or f"Job Title: {job_fields.get('Job Title', '')}"
    prompt = (
        "You are an experienced recruiter. Score how well this candidate matches "
        "the job requirements on a 0-100 scale, where 100 is a perfect fit. Weigh "
        "required qualifications, relevant experience, skills, and education. If the "
        "job description is sparse, infer typical requirements for the role title but "
        "stay conservative. Return an integer score and 1-2 sentences of notes "
        "highlighting the strongest matches and the main gaps.\n\n"
        f"=== JOB ===\n{job_desc}\n\n=== CANDIDATE ===\n{_candidate_text(candidate_fields)}"
    )
    return _generate([prompt], ScoreResult)


# ---------- Drive ----------
def sanitize(s):
    return re.sub(r"[^\w()+\- ]", "_", s).strip()[:120]


def upload_to_drive(drive, pdf_bytes, filename):
    media = MediaInMemoryUpload(pdf_bytes, mimetype="application/pdf", resumable=False)
    f = drive.files().create(
        body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
        media_body=media,
        fields="id,webViewLink",
        supportsAllDrives=True,
    ).execute()
    return f["id"], f.get("webViewLink")


# ---------- Supabase ----------
# Job columns -> the field names _job_text() and the scoring prompt use.
JOB_FIELD_NAMES = {
    "title": "Job Title",
    "status": "Status",
    "role_summary": "Role Summary",
    "core_responsibilities": "Core Responsibilities",
    "requirements_qualifications": "Requirements & Qualifications",
    "experience_required": "Experience Required",
    "education_requirement": "Education Requirement",
}


def recorded_message_ids(msg_ids):
    """Of these Gmail ids, return the ones already stored as candidates."""
    found = set()
    for i in range(0, len(msg_ids), 100):
        chunk = msg_ids[i:i + 100]
        rows = db.select("candidates", {"select": "gmail_message_id",
                                        "gmail_message_id": db.in_list(chunk)})
        found.update(r["gmail_message_id"] for r in rows)
    return found


def parked_message_ids(msg_ids):
    """Of these Gmail ids, return the ones that already failed MAX_ATTEMPTS times."""
    found = set()
    for i in range(0, len(msg_ids), 100):
        chunk = msg_ids[i:i + 100]
        rows = db.select("ingest_failures", {"select": "gmail_message_id",
                                             "gmail_message_id": db.in_list(chunk),
                                             "attempts": f"gte.{MAX_ATTEMPTS}"})
        found.update(r["gmail_message_id"] for r in rows)
    return found


def load_jobs_by_title():
    """Map each live job's exact title -> its fields (for match scoring)."""
    cols = ",".join(JOB_FIELD_NAMES)
    out = {}
    for row in db.select("jobs", {"select": cols, "deleted_at": "is.null"}):
        title = (row.get("title") or "").strip()
        if title:
            out[title] = {JOB_FIELD_NAMES[k]: v for k, v in row.items() if v is not None}
    return out


def create_placeholder_job(job_title, apply_date):
    """Create a minimal job for a role that isn't in the ATS yet.

    Only ever reached from the trusted in-scope Indeed path. The row carries
    the exact same title string written to the candidate's job_title_applied,
    so the dashboard's title-based join links them. It is marked
    PLACEHOLDER_JOB_STATUS and annotated so HR knows to review and complete it.
    Returns the new job id.
    """
    row = db.insert("jobs", {
        "title": job_title,
        "status": PLACEHOLDER_JOB_STATUS,
        "role_summary": (
            "⚠ Auto-created placeholder from an Indeed application received "
            f"on {apply_date}. This role had no posting in the ATS when the "
            "application arrived, so it was created automatically to record the "
            "candidate. Please review and fill in the real job details."
        ),
    })
    return row["id"]


def create_record(row):
    return db.insert("candidates", row, on_conflict="gmail_message_id")["id"]


# ---------- main pipeline ----------
def process(backfill=False, limit=None, dry_run=False):
    gmail, drive = get_services()

    query = 'subject:"New application for" from:indeedemail.com has:attachment'
    if not backfill:
        query += f' -label:{PROCESSED_LABEL}'

    resp = gmail.users().messages().list(userId="me", q=query, maxResults=200).execute()
    msg_ids = [m["id"] for m in resp.get("messages", [])]
    print(f"Found {len(msg_ids)} candidate email(s) matching scope query.")
    if not msg_ids:
        return

    already = set() if dry_run else recorded_message_ids(msg_ids)
    parked = set() if dry_run else parked_message_ids(msg_ids)
    label_id = None if dry_run else ensure_label(gmail, PROCESSED_LABEL)
    jobs_by_title = load_jobs_by_title()

    processed = skipped = failed = 0
    for mid in msg_ids:
        if limit and processed >= limit:
            break
        if mid in already:
            # Recorded but never labelled (e.g. a run died after the insert):
            # label it now so it drops out of the query for good.
            if not backfill:
                gmail.users().messages().modify(
                    userId="me", id=mid, body={"addLabelIds": [label_id]}).execute()
            skipped += 1
            continue
        if mid in parked:
            skipped += 1
            continue
        try:
            if process_message(gmail, drive, mid, jobs_by_title, label_id, dry_run):
                processed += 1
        except Exception as e:
            print(f"    FAILED: {e}")
            failed += 1
            if not dry_run:
                attempts = db.rpc("record_ingest_failure",
                                  {"p_message_id": mid, "p_error": str(e)})
                if attempts >= MAX_ATTEMPTS:
                    print(f"    ! giving up on {mid} after {attempts} attempts "
                          "(see ingest_failures)")

    print(f"\nDone. processed={processed} skipped={skipped} failed={failed}")


def process_message(gmail, drive, mid, jobs_by_title, label_id, dry_run):
    """Ingest one Indeed email. Returns False if it turned out to be out of scope."""
    full = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
    payload = full["payload"]
    if not in_scope(payload):
        return False

    subject = header(payload, "Subject") or ""
    from_hdr = header(payload, "From") or ""
    body = find_plain_text(payload)
    job, location = parse_subject(subject)
    name, indeed_link = parse_body(body)
    if not name:  # fall back to the From display name
        name = re.sub(r"<.*?>", "", from_hdr).strip() or "Unknown"
    apply_date = datetime.fromtimestamp(
        int(full["internalDate"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    orig_name, att_id = find_pdf_attachment(payload)
    att = gmail.users().messages().attachments().get(
        userId="me", messageId=mid, id=att_id).execute()
    pdf_bytes = base64.urlsafe_b64decode(att["data"])

    print(f"\n> {name} - {job} ({apply_date})  [{len(pdf_bytes)} bytes]")

    # AI parse
    try:
        rd = parse_resume(pdf_bytes)
    except Exception as e:
        print(f"    ! resume parse failed: {e}")
        rd = ResumeData()

    # AI match score vs. the matching job's requirements
    cand_for_score = {
        "Candidate Name": name,
        "Years of Experience": rd.years_experience,
        "Key Skills": rd.key_skills,
        "Relevant Experience": rd.relevant_experience,
        "Education & Qualifications": rd.education_qualifications,
        "AI Summary": rd.summary,
    }
    # Resolve the job. If it isn't in the ATS, create a placeholder so the
    # candidate can still be grouped on the dashboard (which joins on title).
    job_key = job.strip()
    job_fields = jobs_by_title.get(job_key)
    if job_fields is None:
        if dry_run:
            print(f"    [dry-run] job '{job_key}' not in ATS — "
                  f"would create a '{PLACEHOLDER_JOB_STATUS}' placeholder")
            job_fields = {"Job Title": job}
        else:
            try:
                new_job_id = create_placeholder_job(job, apply_date)
                print(f"    + created placeholder job '{job_key}' "
                      f"({new_job_id}) — status '{PLACEHOLDER_JOB_STATUS}'")
                job_fields = {"Job Title": job, "Status": PLACEHOLDER_JOB_STATUS}
                # Cache it so other applicants to the same new job in this
                # run reuse the record instead of creating duplicates.
                jobs_by_title[job_key] = job_fields
            except Exception as e:
                print(f"    ! placeholder job create failed: {e}")
                job_fields = {"Job Title": job}
    try:
        sr = score_candidate(cand_for_score, job_fields)
    except Exception as e:
        print(f"    ! scoring failed: {e}")
        sr = None

    fname = sanitize(f"{name} - {job} - {apply_date}") + ".pdf"

    if dry_run:
        print(f"    [dry-run] would upload {fname} to Drive + Supabase, create candidate")
        print(f"    parsed: email={rd.email} phone={rd.phone} "
              f"exp={rd.years_experience} skills={(rd.key_skills or '')[:60]}")
        if sr:
            print(f"    match score: {sr.score} — {sr.notes}")
        return True

    _, drive_link = upload_to_drive(drive, pdf_bytes, fname)
    resume_path = db.upload_resume(pdf_bytes, fname)
    row = {
        "candidate_name": name,
        "status": "New",
        "job_title_applied": job,
        "job_location": location,
        "application_date": apply_date,
        "source": "Indeed",
        "email": rd.email,
        "phone": rd.phone,
        "candidate_location": rd.location,
        "years_of_experience": rd.years_experience,
        "key_skills": rd.key_skills,
        "relevant_experience": rd.relevant_experience,
        "education_qualifications": rd.education_qualifications,
        "ai_summary": rd.summary,
        "resume_path": resume_path,
        "resume_filename": orig_name or fname,
        "resume_drive_link": drive_link,
        "indeed_profile_link": indeed_link,
        "gmail_message_id": mid,
        "match_score": sr.score if sr else None,
        "match_notes": sr.notes if sr else None,
    }
    rec_id = create_record(row)
    gmail.users().messages().modify(
        userId="me", id=mid, body={"addLabelIds": [label_id]}).execute()
    print(f"    OK recorded {rec_id}, resume -> Drive + Supabase, email labelled")
    return True


def main():
    # Windows consoles default to cp1252; force UTF-8 so names/glyphs print.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Indeed -> Supabase/Drive candidate pipeline")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--backfill", action="store_true", help="process all in-scope emails")
    g.add_argument("--run", action="store_true", help="process only un-labelled emails")
    ap.add_argument("--limit", type=int, help="max records to process")
    ap.add_argument("--dry-run", action="store_true", help="parse only; write nothing")
    args = ap.parse_args()

    if not os.environ.get("PIPELINE_OAUTH_JSON") and not TOKEN_FILE.exists():
        sys.exit("Gmail/Drive token missing — set PIPELINE_OAUTH_JSON env var "
                 "or run pipeline_auth.py locally first")

    if args.dry_run:
        process(backfill=args.backfill, limit=args.limit, dry_run=True)
        return
    with db.JobLock(LOCK_NAME, LOCK_TTL_SECONDS) as got:
        if not got:
            print("Another run holds the lock; exiting.")
            return
        process(backfill=args.backfill, limit=args.limit)


if __name__ == "__main__":
    main()
