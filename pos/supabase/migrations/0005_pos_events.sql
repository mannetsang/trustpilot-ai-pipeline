-- Events: 0005
--
-- An event is a show (or any selling period): a name, a currency and a date
-- range. Sales are tagged with the event they were rung under, the register
-- sells under the event chosen in its header, and Analytics reads one event
-- at a time. location_id on sales stays as the event's code for the drawer
-- view and older rows.

create table if not exists pos_events (
  id          uuid primary key default gen_random_uuid(),
  code        text not null unique,                 -- short id, e.g. esi-montreal-2026; written to sales as location_id
  name        text not null,
  starts_on   date not null,
  ends_on     date not null,
  currency    char(3) not null default 'CAD' check (currency in ('CAD', 'USD', 'EUR')),
  is_active   boolean not null default true,        -- false hides it from the register's selector
  created_by  text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint pos_events_dates check (ends_on >= starts_on)
);

comment on table pos_events is
  'Shows and other selling periods. Sales carry event_id; the register sells under the event selected in its header.';

drop trigger if exists pos_events_set_updated_at on pos_events;
create trigger pos_events_set_updated_at
  before update on pos_events
  for each row execute function pos_set_updated_at();

-- The event the register has been selling under so far.
insert into pos_events (code, name, starts_on, ends_on, currency, created_by)
values ('esi-montreal-2026', 'ESI Montreal 2026', date '2026-09-25', date '2026-09-28', 'CAD', 'migration')
on conflict (code) do nothing;

alter table pos_transactions
  add column if not exists event_id uuid references pos_events (id);

update pos_transactions t set event_id = e.id
from pos_events e
where t.event_id is null and t.location_id = e.code;

create index if not exists pos_transactions_event_sold_idx on pos_transactions (event_id, sold_at desc);

alter table pos_events enable row level security;
