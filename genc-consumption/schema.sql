-- GenC Consumption: office-use stock taken out of GenC Beauty inventory.
--
-- Supabase project "GenC Consumption" (ref zitrqrchbdracjvxbwtn, ca-central-1),
-- used by nothing else. Rows entered on the dashboard live here; the rows from
-- before the dashboard stay in the Google Sheet "office use", tab 明细, and are
-- read from there, never copied in.
--
-- Safe to re-run: every statement is `if not exists` or idempotent. Apply with
--   python genc-consumption/setup_db.py

create extension if not exists pgcrypto;

create table if not exists public.consumption (
  id             uuid primary key default gen_random_uuid(),
  entry_date     date not null,                                   -- Date
  sku            text not null check (length(sku) between 1 and 300),
  reason         text not null check (length(reason) between 1 and 50),   -- Note (视频需要, 样品需要, 客户需求, 跨部门)
  qty            numeric(12, 2) not null check (qty > 0),
  -- The cost per unit when the line was entered, from the Cost Ref tab (cost_source
  -- 'cost_ref'), typed by hand for a SKU Cost Ref does not have ('manual'), or
  -- unknown ('none'). Kept, so a later price change does not rewrite past months.
  unit_cost_cad  numeric(12, 4) check (unit_cost_cad is null or unit_cost_cad >= 0),
  cost_source    text not null default 'none' check (cost_source in ('cost_ref', 'manual', 'none')),
  subtotal_cad   numeric(14, 4) generated always as (qty * unit_cost_cad) stored,
  request_by     text not null default '' check (length(request_by) <= 80),
  sent_to        text not null default '' check (length(sent_to) <= 200),
  process_date   date,
  picked_up      boolean not null default false,                  -- 拿货
  issued         boolean not null default false,                  -- 出库
  created_by     text not null,
  created_at     timestamptz not null default now(),
  updated_by     text,
  updated_at     timestamptz not null default now(),
  deleted_by     text,
  deleted_at     timestamptz
);

create index if not exists consumption_entry_date_idx on public.consumption (entry_date) where deleted_at is null;

-- Only the server reaches the table, with the service-role key (which bypasses
-- RLS). RLS on with no policies keeps the anon and authenticated keys out.
alter table public.consumption enable row level security;
revoke all on public.consumption from anon, authenticated;
grant select, insert, update, delete on public.consumption to service_role;
