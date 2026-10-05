# Product Requirements Document — Superhairpieces Jobs Dashboard (ATS)

> **Status:** Reverse-engineered from the shipped implementation (`alinayangg/jobs-dashboard`), 2026-07-24.
> This documents *what the product does today*, framed as requirements, so it can serve as a baseline for future changes.

---

## 1. Overview

The Jobs Dashboard is a lightweight Applicant Tracking System (ATS) front-end for Superhairpieces (New Frontier Global). It gives the HR/Administration team a single web page to manage open roles and review candidates whose résumés are automatically ingested from Indeed by an upstream pipeline.

It is the **human-facing layer** of a larger recruiting stack: an automated pipeline (`indeed-pipeline`, a Cloud Run Job) parses incoming Indeed applications, scores them with AI, and writes them to Airtable; this dashboard reads and acts on that data.

- **Type:** Single-page web app (React + Vite + TypeScript), served by a small Node proxy.
- **Data store:** Airtable base "HR Manager" (`appar5DLoak36lfyj`).
- **Hosting:** Cloud Run Service, region `northamerica-northeast2`.
- **Live URL:** https://jobs-dashboard-304363458561.northamerica-northeast2.run.app/

## 2. Goals & Non-Goals

### Goals
- Give HR one place to see all job postings and their live status.
- Surface every candidate against the role they applied for, ranked by an AI match score.
- Let HR triage without touching Airtable directly: change job status, reassign candidates, delete stale postings, create new postings.
- Let HR initiate a hire by capturing offer details and generating a ready-to-review offer/hire email — without ever auto-sending.

### Non-Goals (as currently built)
- **No authentication / access control.** The app and its API endpoints are open to anyone with the URL (explicitly flagged in `server.mjs` and `mail.mjs` as needing IAP / app-level login before wide rollout).
- **No résumé ingestion or AI scoring.** That is owned by the upstream `indeed-pipeline`; the dashboard only consumes its output.
- **No automated email sending.** The product deliberately creates Gmail *drafts* only.
- No multi-stage interview workflow, scheduling, notes/comments, or audit log.
- No candidate-facing views.

## 3. Users & Personas

- **Primary user:** HR / Administration staff (e.g. Head of Administration). Technical comfort: moderate; expected to work in a browser, not in Airtable or code.
- **Implicit stakeholders:** Hiring managers (consume outcomes), candidates (receive the hire email the tool drafts).

## 4. System Context / Architecture

```
Indeed application emails
        │
        ▼
  indeed-pipeline  ──►  Google Drive (résumé PDF)
  (Cloud Run Job)  ──►  Gemini résumé parse + AI match score
        │
        ▼
   Airtable "HR Manager" base
   ├─ Jobs table        (tblEPFbViaY4EpjF8)
   ├─ Candidates table  (tbl4I3BpES6LDla89)
   └─ Employee table    (tblZ38T0qi31dW4jD)
        ▲
        │  (all reads/writes)
        │
  server.mjs  ── proxies /api/airtable/*  (injects AIRTABLE_TOKEN server-side)
             ── handles  /api/send-email  (Gmail draft via mail.mjs)
             ── serves    static Vite build (./dist)
        ▲
        │
  React SPA (App.tsx)  ── the dashboard UI
```

Key architectural decisions:
- **Secrets never reach the browser.** The Airtable token and Gmail OAuth credentials live only in the server process (env vars / Secret Manager), injected by the proxy.
- **Same handler in dev and prod.** The `/api/send-email` logic (`mail.mjs`) is shared between the Vite dev-server middleware (`vite.config.ts`) and the production `server.mjs`.
- **Pure-Node mail path.** Gmail drafting uses the Gmail REST API directly (no Python, no extra runtime) so it runs in a `node:slim` Cloud Run container.

## 5. Data Model

### Jobs (`tblEPFbViaY4EpjF8`)
Job Title, Status, Date Posted, Employment Type, Work Arrangement, Department, Company / Brand, Location, Schedule, Pay Min, Pay Max, Pay Period, Pay Currency, Education Requirement, Experience Required, Benefits, Company Overview, Role Summary, Core Responsibilities, Requirements & Qualifications.

- **Status** enum: Draft, Open, On Hold, Filled, Closed.
- **Department** enum (in Add-Job form): Customer Service, Sales, Warehouse / Logistics, Administration, Marketing, Manufacturing.

### Candidates (`tbl4I3BpES6LDla89`) — populated by the pipeline
Candidate Name, Status, **Job Title Applied**, Job Location, Application Date, Source, Email, Phone, Candidate Location, Years of Experience, Key Skills, Relevant Experience, Education & Qualifications, AI Summary, Résumé (attachment), Résumé Drive Link, Indeed Profile Link, **Match Score**, Match Notes.

- **Candidate Status** enum (styled in UI): New, Reviewing, Screening, Interview, Offer, Hired, Rejected.

### Employee (`tblZ38T0qi31dW4jD`) — written on hire
Candidate Name, Email, Job Title, Compensation Type, Compensation Amount, Location, Employment Type, Start Date, Probation Period (Months), Offer Emailed At.

### The critical join
Candidates are matched to Jobs by an **exact, trimmed, case-sensitive string match** between candidate `Job Title Applied` and job `Job Title`. A candidate whose applied title has no matching Jobs record does not appear under any job. *(This is a known fragility; the upstream pipeline mitigates it by creating a "Needs Review" placeholder job for unposted titles.)*

## 6. Functional Requirements

### 6.1 Dashboard / summary
- On load, fetch all Jobs (sorted by Date Posted desc) and all Candidates (sorted by Application Date desc) in parallel, paginating through Airtable 100 at a time.
- Show three summary tiles: **Total jobs**, **Open positions** (Status = Open), **Departments hiring** (distinct non-empty Department across all jobs).
- Provide a **Refresh** button to re-pull both tables; show a spinner while loading.
- Surface any fetch/write error in a dismissible banner.

### 6.2 Jobs table
- List jobs in a table: Job Title (click to open candidates), Department, Status, Type, Location, Pay, Posted date, Candidate count, Delete.
- **Filters:** by Status and by Location (locations derived from the data); "Clear" resets both. Header reflects "Showing X of Y jobs" when filtered.
- **Inline status change:** each row's Status is an editable dropdown; changing it PATCHes Airtable with an *optimistic* update (revert + error banner on failure); per-row spinner while saving.
- **Pay formatting:** currency symbol by code (CA$, €, else $), min–max range (or single value), lowercased pay period; "—" when no pay set.
- **Candidate count** per job = number of candidates whose `Job Title Applied` matches that job's title. Clicking the count (or the title) opens the candidates dialog.
- **Delete job:** confirmation dialog (AlertDialog) warning it removes the Airtable record (restorable from Airtable trash for a limited time); optimistic removal from the list.
- Empty/edge states: "No jobs yet…", "No jobs match the current filters."

### 6.3 Add a job
- "Add job" opens a form dialog that creates a Jobs record.
- Fields: Job title (required), Department, Status (default Open), Employment type (default Full-time), Work arrangement (default In person), Location, Schedule, Pay min/max, Pay period (default Per hour), Pay currency (default CAD), Date posted (defaults to today), Role summary.
- Company / Brand is hard-set to "New Frontier Global / Superhairpieces".
- Uses Airtable `typecast: true` so new single-select option values can be created on the fly.
- New job is prepended to the list on success.

### 6.4 Candidate review (per job)
- Opening a job shows its candidates in a dialog, **sorted by Match Score descending**.
- Each candidate card shows: Match Score badge (color-graded — green ≥80, lime ≥60, amber ≥40, red below), name, candidate Status badge, Match Notes, years of experience, location, email, phone, Key Skills, AI Summary.
- **Links:** résumé attachment (opens file), Résumé Drive Link, Indeed Profile Link — each shown only if present.
- **Move to job:** a dropdown reassigns the candidate to a different existing job title (PATCHes `Job Title Applied`, optimistic, with revert on failure). The current job is excluded from the list.
- Empty state: "No candidates have applied for this position yet."

### 6.5 Prepare offer & draft hire email
- Each candidate with an email shows a **"Draft hire email"** button (disabled with tooltip if no email on file).
- Clicking opens an **offer-details** dialog, pre-filled from the job where possible:
  - Compensation type (Hourly/Salary — inferred from job Pay Period), Amount (required), Location (required; fixed list of company sites), Employment type (Full-time/Part-time), Start date (required), Probation period (1–12 months, default 3).
- On submit:
  1. Create an **Employee** record with the offer details plus `Offer Emailed At` = now.
  2. Compose a fixed-template hire email (congratulations + request for full mailing address to finalize the offer letter), personalized with first name and role.
  3. POST to `/api/send-email`, which creates a **Gmail draft** (never sends) in the connected account, best-effort attaching the account's Gmail signature.
- On success the button becomes "Draft created"; errors are surfaced per-candidate.
- **Explicit product promise (shown in the dialog): "Nothing is sent automatically."** The user reviews and sends the draft themselves.

## 7. Non-Functional Requirements

- **Security posture (current):** No auth on the SPA or the `/api/airtable` and `/api/send-email` endpoints. Both are documented as needing IAP / app-level login before wide rollout, since the mail endpoint could otherwise be used as an open relay. **This is the top hardening item.**
- **Secret handling:** Airtable token and Gmail OAuth creds only server-side; multiple resolution paths (env / base64 token / local `token.json`).
- **Resilience:** optimistic UI updates with rollback on all mutations; access-token caching with 60s expiry margin; SPA fallback routing; graceful handling of non-JSON error bodies.
- **Deployment:** Dockerized (`node:slim`), Cloud Run Service; deploy via `deploy.sh` / `deploy.ps1`.
- **Performance:** all data pulled client-side on load; fine at current record volumes, no server-side filtering/caching.

## 8. Known Limitations & Risks

1. **No access control** — anyone with the URL can read all candidate PII and draft mail as the connected account.
2. **Brittle title join** — case/whitespace-sensitive matching silently hides candidates; depends on the pipeline's placeholder-job workaround.
3. **Full-table client fetch** — no pagination/search in the UI; will degrade as Jobs/Candidates grow.
4. **No de-dup or activity history** — no audit trail of status changes, moves, or drafts created.
5. **Hire email is a single fixed template** — no per-role customization beyond name/title.
6. **"Draft created" state is session-only** — not persisted; a refresh loses which candidates already have drafts.

## 9. Opportunities / Suggested Next Steps

- Add authentication (Google IAP or app-level login) — prerequisite for any wider use.
- Make the candidate↔job join resilient (normalize case/whitespace, or use Airtable linked records instead of title strings).
- Persist "offer drafted" state (e.g. read back `Offer Emailed At` / a candidate Status of Offer) so it survives refresh.
- Server-side search/pagination as data grows.
- Optional: move a candidate's Status through the pipeline (New → … → Hired) from the UI, not just reassignment.
```
