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
  document_type    text,                      -- invoice | receipt | payment_confirmation | credit_note | statement | other
  removed_at       timestamptz,               -- set when the file is no longer found in Drive
  created_at       timestamptz not null default now()
);
alter table invoices add column if not exists document_type text;
alter table invoices add column if not exists removed_at timestamptz;
-- The card transactions the document lists ([{"date", "amount", "card_last4"}], e.g. an Amazon order's
-- "Credit Card transactions"); null for a reading made before this column existed.
alter table invoices add column if not exists card_charges jsonb;
-- charge_date: when the document says its total is charged to the card automatically (a pre-authorized
-- bill: TELUS prints "will be charged to your credit card on October 09", or "15 days from your bill");
-- the matcher compares a purchase with this date instead of invoice_date. null when it says nothing.
alter table invoices add column if not exists charge_date date;
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
  raw               jsonb,
  rejected_invoice_ids text[]                        -- invoices a person said are "not this one"
);
alter table transactions add column if not exists rejected_invoice_ids text[];
create index if not exists transactions_month_idx on transactions (year, month, card_last4);
-- invoice_part: which of the invoice's listed card charges (its index in invoices.card_charges) the
-- transaction is; null for a link to the whole invoice. An invoice that lists several charges (an Amazon
-- order shipped in parts) is shared that way, one transaction per charge; any other invoice is linked to
-- at most one transaction.
alter table transactions add column if not exists invoice_part integer;
-- People a transaction can be assigned to (who made the purchase, or answers for it). Anyone who can
-- open the dashboard may add one; names are unique ignoring case. person_id is cleared if the person
-- is deleted.
create table if not exists people (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  email      text,
  created_by text,
  created_at timestamptz not null default now()
);
create unique index if not exists people_name_unique on people (lower(name));
alter table transactions add column if not exists person_id uuid references people(id) on delete set null;
create index if not exists transactions_person_idx on transactions (person_id);
drop index if exists transactions_invoice_unique;
create unique index if not exists transactions_invoice_whole_unique on transactions (invoice_id)
  where invoice_id is not null and invoice_part is null;
create unique index if not exists transactions_invoice_part_unique on transactions (invoice_id, invoice_part)
  where invoice_id is not null and invoice_part is not null;
-- also_invoice_ids: the other documents the one charge pays or is evidenced by, next to invoice_id: the
-- second and third invoice of a payment that settles several at once (UPS charges a week's customs-brokerage
-- invoices as one; Purolator's billing centre three weekly invoices), or the payment confirmation of a charge
-- linked to its invoice. They count as linked: never another row's invoice_id or also document.
alter table transactions add column if not exists also_invoice_ids text[] not null default '{}';
create index if not exists transactions_also_invoice_idx on transactions using gin (also_invoice_ids);
alter table transactions drop constraint if exists transactions_also_needs_invoice;
alter table transactions add constraint transactions_also_needs_invoice
  check (invoice_id is not null or cardinality(also_invoice_ids) = 0);
-- ... and never both: an invoice linked whole is not also linked by charge, nor one row's also document
-- another row's invoice or also document. The locks (taken in one order: no two writers can deadlock)
-- serialize two writers of the same document, so neither passes the check while the other's row is uncommitted.
create or replace function transactions_invoice_link_guard() returns trigger language plpgsql as $$
declare
  doc text;
begin
  for doc in select distinct d from unnest(array[new.invoice_id] || coalesce(new.also_invoice_ids, '{}')) as d
             where d is not null order by d loop
    perform pg_advisory_xact_lock(hashtext('transactions.invoice_id:' || doc));
  end loop;
  if new.invoice_id is not null then
    if exists (select 1 from transactions o
               where o.invoice_id = new.invoice_id and o.id <> new.id
                 and (o.invoice_part is null) <> (new.invoice_part is null)) then
      raise exception 'invoice % is already linked to another transaction (whole and by charge)', new.invoice_id
        using errcode = 'unique_violation';
    end if;
    if exists (select 1 from transactions o
               where o.id <> new.id and o.also_invoice_ids @> array[new.invoice_id]) then
      raise exception 'invoice % is already linked to another transaction (paid with its invoice)', new.invoice_id
        using errcode = 'unique_violation';
    end if;
  end if;
  foreach doc in array coalesce(new.also_invoice_ids, '{}') loop
    if doc = new.invoice_id or exists (select 1 from transactions o
                                       where o.id <> new.id and (o.invoice_id = doc or o.also_invoice_ids @> array[doc])) then
      raise exception 'invoice % is already linked to another transaction', doc
        using errcode = 'unique_violation';
    end if;
  end loop;
  return new;
end $$;
drop trigger if exists transactions_invoice_link_guard on transactions;
create trigger transactions_invoice_link_guard
  before insert or update of invoice_id, invoice_part, also_invoice_ids on transactions
  for each row execute function transactions_invoice_link_guard();
-- paid_invoice_numbers: for a payment confirmation (document_type payment_confirmation: UPS's "Payment
-- Confirmation", a billing centre's receipt), the invoice numbers it says the payment settles.
alter table invoices add column if not exists paid_invoice_numbers text[];

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

-- The Drive import (`python main.py sync`, every 15 minutes): one row per file found in the Bank
-- Transactions folder, so each version of a file is read once and a file that could not be read
-- says why. modified_time is the version read; status imported | failed | retrying (a failure
-- that was not about the file's content, tried again on the next run).
create table if not exists statement_sources (
  file_id           text primary key,
  name              text not null,
  path              text not null default '',      -- subfolders between the watched folder and the file
  mime_type         text,
  web_link          text,
  modified_time     timestamptz,
  status            text not null,
  statement_id      uuid references statements(id) on delete set null,
  transaction_count integer not null default 0,
  new_count         integer not null default 0,
  months            text[] not null default '{}',
  transaction_ids   text[] not null default '{}',  -- the lines of the version read, to see what a new one dropped
  attempts          integer not null default 0,
  error             text,
  warnings          text[] not null default '{}',
  checked_at        timestamptz not null default now()
);

-- One lease for the long steps (upload, read invoices, match, remind, the Drive import). The
-- dashboard's own steps also share an in-process lock; the Cloud Run jobs run in other containers,
-- so they meet here. A holder renews it every minute; one that died loses it when it expires.
create table if not exists job_lease (
  name        text primary key,
  holder      text,
  job         text,
  acquired_at timestamptz,
  expires_at  timestamptz not null default '1970-01-01T00:00:00Z'
);
insert into job_lease (name) values ('jobs') on conflict do nothing;

-- Small shared state as JSON: "bank_sync" is the Drive import's last check, for the dashboard.
create table if not exists app_state (
  key        text primary key,
  value      jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

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
-- Run the view with the caller's rights (so RLS applies to it too) and keep the
-- public API roles away from everything: only the service-role key is used.
alter view month_summary set (security_invoker = true);
revoke all on cards, statements, invoices, transactions, notifications, cost_centers, month_summary,
  statement_sources, job_lease, app_state, people
  from anon, authenticated;

-- The service uses the service-role key, which bypasses RLS. Enabling RLS with
-- no policies keeps the anon/public key from reading anything.
alter table cards         enable row level security;
alter table statements    enable row level security;
alter table invoices      enable row level security;
alter table transactions  enable row level security;
alter table notifications enable row level security;
alter table cost_centers  enable row level security;
alter table statement_sources enable row level security;
alter table job_lease     enable row level security;
alter table app_state     enable row level security;
alter table people        enable row level security;
