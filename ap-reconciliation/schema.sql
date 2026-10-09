-- AP credit-card reconciliation: Supabase (Postgres) schema.
-- Run once in the Supabase SQL editor of the dedicated project, or apply with
--   python ap-reconciliation/setup_db.py --project-ref <ref>
-- Re-running is safe: every statement is idempotent.

create extension if not exists pgcrypto;

-- One row per credit card seen on a statement. The Chat webhook for the card
-- is NOT here (it is a secret, CARD_WEBHOOKS_JSON in Secret Manager); this
-- holds the display label, the owner who may code the card's transactions,
-- and the name of the Chat space for reference.
create table if not exists cards (
  last4        text primary key,
  label        text,
  holder_name  text,
  owner_email  text,
  chat_space   text,
  active       boolean not null default true,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz
);

create table if not exists statements (
  id                uuid primary key default gen_random_uuid(),
  file_name         text not null,
  file_type         text,
  format            text,
  uploaded_by       text,
  uploaded_at       timestamptz not null default now(),
  period_start      date,
  period_end        date,
  cards             text[] not null default '{}',
  months            text[] not null default '{}',   -- 'YYYY MM' month folders touched
  transaction_count integer not null default 0,
  new_count         integer not null default 0,
  drive_file_id     text,
  drive_link        text,
  warnings          text[] not null default '{}'
);

create table if not exists invoices (
  id               text primary key,          -- Drive file id
  file_name        text not null,
  mime_type        text,
  web_view_link    text,
  folder_path      text,
  month_folder     text,                      -- 'YYYY MM'
  vendor_folder    text,
  modified_time    text,                      -- Drive modifiedTime, re-read when it changes
  size             bigint,
  vendor           text,
  invoice_number   text,
  invoice_date     date,
  total            numeric(12,2),
  currency         text,
  card_last4       text,
  payment_method   text,
  summary          text,
  is_invoice       boolean,
  extracted_at     timestamptz,
  extraction_error text,
  created_at       timestamptz not null default now()
);
create index if not exists invoices_month_folder_idx on invoices (month_folder);

create table if not exists transactions (
  id                text primary key,          -- deterministic hash of the statement line
  statement_id      uuid references statements(id) on delete set null,
  card_last4        text not null references cards(last4),
  holder_name       text,
  txn_date          date not null,
  post_date         date,
  year              integer not null,
  month             integer not null,
  description       text not null,
  supplier          text,
  city              text,
  country           text,
  merchant_category text,
  type              text not null default 'purchase',   -- purchase | credit | payment | fee
  amount            numeric(12,2) not null,             -- billing currency; purchases positive
  currency          text not null default 'CAD',
  source_amount     numeric(12,2),
  source_currency   text,
  invoice_status    text not null default 'missing',    -- matched | possible | missing | waived | not_required
  invoice_id        text references invoices(id) on delete set null,
  match_confidence  numeric(4,2),
  match_method      text,                               -- rule | ai | manual
  match_note        text,
  cost_center       text,
  usage             text,
  note              text,
  updated_by        text,
  updated_at        timestamptz,
  created_at        timestamptz not null default now(),
  raw               jsonb
);
create index if not exists transactions_month_idx on transactions (year, month, card_last4);
create unique index if not exists transactions_invoice_unique on transactions (invoice_id) where invoice_id is not null;

create table if not exists notifications (
  id              bigserial primary key,
  card_last4      text,
  year            integer,
  month           integer,
  sent_at         timestamptz not null default now(),
  sent_by         text,
  missing_count   integer,
  possible_count  integer,
  transaction_ids text[] not null default '{}',
  ok              boolean,
  detail          text
);

create table if not exists cost_centers (
  name text primary key,
  sort integer not null default 100
);
insert into cost_centers (name, sort) values
  ('Marketing', 10), ('IT & Software', 20), ('Shipping & Logistics', 30), ('Travel', 40),
  ('Meals & Entertainment', 50), ('Office & Supplies', 60), ('Inventory / COGS', 70),
  ('Professional Services', 80), ('Utilities & Telecom', 90), ('Insurance & Government', 100),
  ('Salon - Dufferin', 110), ('Salon - Ridgeway', 111), ('Salon - Consumer', 112), ('Salon - Eglinton', 113),
  ('Salon - STC', 114), ('Salon - Rapistan', 115), ('Salon - Brampton', 116), ('US', 120), ('EU', 121),
  ('Other', 999)
on conflict (name) do nothing;

-- Per-month, per-card roll-up for the dashboard's year/month navigation.
create or replace view month_summary as
select year, month, card_last4,
       count(*)                                                              as transactions,
       coalesce(sum(amount) filter (where type = 'purchase'), 0)             as spend,
       count(*) filter (where invoice_status = 'matched')                    as matched,
       count(*) filter (where invoice_status = 'possible')                   as possible,
       count(*) filter (where invoice_status = 'missing')                    as missing,
       count(*) filter (where invoice_status = 'waived')                     as waived,
       count(*) filter (where type = 'purchase' and coalesce(cost_center, '') = '') as uncoded
from transactions
group by year, month, card_last4;

-- The service uses the service-role key, which bypasses RLS. Enabling RLS with
-- no policies keeps the anon/public key from reading anything.
alter table cards         enable row level security;
alter table statements    enable row level security;
alter table invoices      enable row level security;
alter table transactions  enable row level security;
alter table notifications enable row level security;
alter table cost_centers  enable row level security;
