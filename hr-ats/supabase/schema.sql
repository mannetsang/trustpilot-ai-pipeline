-- HR applicant tracking (ATS) schema for Supabase project `shp-ats`
-- (ref qxmwygkwctyksfcmsqsf, ca-central-1). Replaces the Airtable base
-- "HR Manager" (appar5DLoak36lfyj).
--
-- Column names are snake_case versions of the Airtable field names. Every
-- table keeps the record's Airtable id and its raw Airtable fields
-- (`airtable_fields`) so the import is lossless even for fields that have no
-- column of their own.
--
-- Access: RLS is on with no policies, so the anon/authenticated keys can read
-- nothing. Only the server-side secret key (dashboard server, pipeline jobs)
-- gets through. Candidate data is personal data; keep it that way.
--
-- Idempotent: safe to re-run.

create extension if not exists pgcrypto;

create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

-- ---------------------------------------------------------------- jobs
create table if not exists jobs (
  id                           uuid primary key default gen_random_uuid(),
  title                        text not null,
  status                       text,          -- Draft, Open, On Hold, Filled, Closed, Needs Review
  date_posted                  date,
  employment_type              text,
  work_arrangement             text,
  department                   text,
  company_brand                text,
  location                     text,
  schedule                     text,
  pay_min                      numeric,
  pay_max                      numeric,
  pay_period                   text,
  pay_currency                 text,
  education_requirement        text,
  experience_required          text,
  benefits                     text[],
  company_overview             text,
  role_summary                 text,
  core_responsibilities        text,
  requirements_qualifications  text,
  airtable_id                  text unique,
  airtable_fields              jsonb,
  created_at                   timestamptz not null default now(),
  updated_at                   timestamptz not null default now(),
  deleted_at                   timestamptz    -- soft delete from the dashboard
);
create index if not exists jobs_title_idx on jobs (title) where deleted_at is null;
drop trigger if exists jobs_updated_at on jobs;
create trigger jobs_updated_at before update on jobs
  for each row execute function set_updated_at();

-- ---------------------------------------------------------- candidates
-- Candidates join to jobs by title (job_title_applied = jobs.title), exactly
-- as the dashboard did on Airtable.
create table if not exists candidates (
  id                          uuid primary key default gen_random_uuid(),
  candidate_name              text,
  status                      text,          -- New, Reviewing, Screening, Interview, Offer, Hired, Rejected
  job_title_applied           text,
  job_location                text,
  application_date            date,
  source                      text,
  email                       text,
  phone                       text,
  candidate_location          text,
  years_of_experience         numeric,
  key_skills                  text,
  relevant_experience         text,
  education_qualifications    text,
  ai_summary                  text,
  resume_path                 text,          -- object path in the private `resumes` bucket
  resume_filename             text,
  resume_drive_link           text,
  indeed_profile_link         text,
  gmail_message_id            text unique,   -- idempotency key for the Indeed pipeline
  match_score                 integer,
  match_notes                 text,
  airtable_id                 text unique,
  airtable_fields             jsonb,
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now()
);
create index if not exists candidates_job_title_idx on candidates (job_title_applied);
create index if not exists candidates_application_date_idx on candidates (application_date desc);
drop trigger if exists candidates_updated_at on candidates;
create trigger candidates_updated_at before update on candidates
  for each row execute function set_updated_at();

-- ----------------------------------------------------------- employees
-- Written by the dashboard's "Draft hire email" flow; read by offer-packet.
create table if not exists employees (
  id                       uuid primary key default gen_random_uuid(),
  candidate_name           text,
  email                    text,
  job_title                text,
  compensation_type        text,             -- Hourly, Salary
  compensation_amount      numeric,
  location                 text,
  employment_type          text,
  start_date               date,
  probation_period_months  integer,
  offer_emailed_at         timestamptz,
  offer_packet_sent_at     timestamptz,
  airtable_id              text unique,
  airtable_fields          jsonb,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);
create index if not exists employees_email_idx on employees (lower(email));
drop trigger if exists employees_updated_at on employees;
create trigger employees_updated_at before update on employees
  for each row execute function set_updated_at();

-- ------------------------------------------------------ company_offices
-- Location name (as picked in the offer dialog) -> full mailing address for
-- the offer letter.
create table if not exists company_offices (
  id               uuid primary key default gen_random_uuid(),
  location         text not null unique,
  address          text,
  airtable_id      text unique,
  airtable_fields  jsonb,
  created_at       timestamptz not null default now()
);

-- ------------------------------------------------ insurance_eligibility
-- Start-date range (from the 21st of `from_label`'s month) -> the date the
-- health insurance starts. Labels kept as Airtable wrote them ("June 01").
create table if not exists insurance_eligibility (
  id                   uuid primary key default gen_random_uuid(),
  from_label           text not null,
  eligible_date_label  text not null,
  airtable_id          text unique,
  airtable_fields      jsonb,
  created_at           timestamptz not null default now()
);

-- --------------------------------------------- pipeline bookkeeping
-- Indeed emails that failed to ingest. The pipeline gives up on a message
-- after a few attempts so one bad email can't be re-parsed by Gemini every
-- run forever (what happened while Airtable was full).
create table if not exists ingest_failures (
  gmail_message_id  text primary key,
  attempts          integer not null default 1,
  last_error        text,
  first_failed_at   timestamptz not null default now(),
  last_failed_at    timestamptz not null default now()
);

-- Run lock. Cloud Scheduler fires the jobs more often than a run can take,
-- so each run takes this lease first and exits if another run holds it.
create table if not exists job_locks (
  name          text primary key,
  holder        text not null,
  locked_until  timestamptz not null
);

create or replace function try_job_lock(p_name text, p_holder text, p_ttl_seconds integer)
returns boolean language plpgsql security definer set search_path = public as $$
declare got boolean;
begin
  insert into job_locks (name, holder, locked_until)
  values (p_name, p_holder, now() + make_interval(secs => p_ttl_seconds))
  on conflict (name) do update
    set holder = excluded.holder, locked_until = excluded.locked_until
    where job_locks.locked_until < now() or job_locks.holder = excluded.holder
  returning true into got;
  return coalesce(got, false);
end $$;

create or replace function release_job_lock(p_name text, p_holder text)
returns void language sql security definer set search_path = public as $$
  delete from job_locks where name = p_name and holder = p_holder;
$$;

create or replace function record_ingest_failure(p_message_id text, p_error text)
returns integer language sql security definer set search_path = public as $$
  insert into ingest_failures (gmail_message_id, last_error)
  values (p_message_id, left(p_error, 2000))
  on conflict (gmail_message_id) do update
    set attempts = ingest_failures.attempts + 1,
        last_error = excluded.last_error,
        last_failed_at = now()
  returning attempts;
$$;

revoke execute on function try_job_lock(text, text, integer) from public, anon, authenticated;
revoke execute on function release_job_lock(text, text) from public, anon, authenticated;
revoke execute on function record_ingest_failure(text, text) from public, anon, authenticated;

-- ---------------------------------------------------------------- RLS
alter table jobs                  enable row level security;
alter table candidates            enable row level security;
alter table employees             enable row level security;
alter table company_offices       enable row level security;
alter table insurance_eligibility enable row level security;
alter table ingest_failures       enable row level security;
alter table job_locks             enable row level security;

-- ------------------------------------------------------------ storage
-- Résumé PDFs. Private: the dashboard hands out short-lived signed URLs.
-- No MIME restriction: the pipeline only takes PDFs, but résumés imported
-- from Airtable can be Word files.
insert into storage.buckets (id, name, public, file_size_limit)
values ('resumes', 'resumes', false, 20971520)
on conflict (id) do nothing;
