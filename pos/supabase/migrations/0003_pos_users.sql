-- POS users: 0003
--
-- Staff accounts for the register and the admin portal. Each person has a
-- name and a personal PIN (stored hashed, never in clear); admins can also
-- open the admin portal and approve refunds. The first admin is created
-- through the portal using the master code held in Secret Manager.

create table if not exists pos_users (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  role          text not null default 'cashier' check (role in ('cashier', 'admin')),
  pin_hash      text not null,                 -- algorithm$salt$hash, see main.py
  is_active     boolean not null default true,
  last_login_at timestamptz,
  created_by    text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Names are how staff pick themselves on the lock screen, so no two active
-- users may share one (case-insensitively).
create unique index if not exists pos_users_active_name_idx on pos_users (lower(name)) where is_active;

comment on table pos_users is
  'Register and admin portal accounts. PINs are salted hashes; role admin opens the portal and approves refunds.';

drop trigger if exists pos_users_set_updated_at on pos_users;
create trigger pos_users_set_updated_at
  before update on pos_users
  for each row execute function pos_set_updated_at();

-- Sales remember which account rang them, alongside the name captured as text.
alter table pos_transactions
  add column if not exists staff_user_id uuid references pos_users (id);

create index if not exists pos_transactions_staff_user_idx on pos_transactions (staff_user_id);

alter table pos_users enable row level security;
