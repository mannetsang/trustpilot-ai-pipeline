-- POS cash transactions: 0001
--
-- One row per completed sale at the till, one row per line sold. Cash only for
-- now; the payment_method check is the single place to widen when card, gift
-- card or split tenders arrive. Customers are deliberately not modelled.
--
-- Money is numeric(12,2) in the transaction's own currency. Never sum across
-- currencies: a CAD till and a USD till are two reports.
--
-- Applied by pos/apply_migration.py, which records each file here once.

create table if not exists pos_schema_migrations (
  name        text primary key,
  applied_at  timestamptz not null default now()
);

-- Receipt numbers. Sequential and never reused, so a printed receipt can be
-- found again. Starts above the numbers the design prototype showed.
create sequence if not exists pos_transaction_number_seq start with 10001;

create table if not exists pos_transactions (
  id                  uuid primary key default gen_random_uuid(),
  transaction_number  bigint not null unique default nextval('pos_transaction_number_seq'),

  -- Set by the register before it sends. The register queues sales offline and
  -- retries, so a retry of the same sale must not create a second row.
  client_record_id    text unique,

  status              text not null default 'completed'
                      check (status in ('completed', 'inspection', 'refunded', 'void')),

  -- Where and who. Location codes are short text ids (the design uses e.g.
  -- 'stc'); a locations table can be added and referenced later.
  location_id         text not null,
  register_id         text,
  staff_name          text not null,

  currency            char(3) not null check (currency in ('CAD', 'USD', 'EUR')),

  -- Tender. Cash only for now: widen this check to add card / gift / split.
  payment_method      text not null default 'cash' check (payment_method in ('cash')),

  -- Totals, all in `currency`.
  subtotal            numeric(12,2) not null check (subtotal >= 0),
  discount_total      numeric(12,2) not null default 0 check (discount_total >= 0),
  coupon_code         text,
  tax_rate            numeric(7,5)  not null check (tax_rate >= 0 and tax_rate < 1),
  tax_label           text,                       -- e.g. 'HST 13%', printed on the receipt
  tax_total           numeric(12,2) not null default 0 check (tax_total >= 0),
  grand_total         numeric(12,2) not null check (grand_total >= 0),

  -- Inspection orders take a deposit today (the cheapest hairpiece in the
  -- sale) and settle the balance later. amount_paid is what actually changed
  -- hands at this transaction.
  is_inspection       boolean not null default false,
  deposit_total       numeric(12,2) check (deposit_total is null or deposit_total >= 0),
  amount_paid         numeric(12,2) not null check (amount_paid >= 0),

  -- Cash drawer detail. Required whenever payment_method = 'cash'.
  cash_received       numeric(12,2) check (cash_received is null or cash_received >= 0),
  change_given        numeric(12,2) not null default 0 check (change_given >= 0),

  -- Refunds are recorded on the sale they reverse. A partial refund leaves the
  -- status as it was; a full one sets it to 'refunded'.
  refund_amount       numeric(12,2) check (refund_amount is null or refund_amount > 0),
  refund_method       text check (refund_method is null or refund_method in ('cash', 'store_credit')),
  refund_reason       text,
  refund_approved_by  text,
  refunded_at         timestamptz,

  note                text,

  sold_at             timestamptz not null default now(),
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  constraint pos_transactions_totals_add_up
    check (grand_total = round(subtotal - discount_total + tax_total, 2)),
  constraint pos_transactions_discount_within_subtotal
    check (discount_total <= subtotal),
  constraint pos_transactions_paid_matches_deposit_rule
    check (
      (is_inspection and deposit_total is not null and amount_paid = deposit_total)
      or (not is_inspection and deposit_total is null and amount_paid = grand_total)
    ),
  constraint pos_transactions_cash_detail
    check (
      payment_method <> 'cash'
      or (cash_received is not null
          and cash_received >= amount_paid
          and change_given = round(cash_received - amount_paid, 2))
    ),
  constraint pos_transactions_refund_within_paid
    check (refund_amount is null or refund_amount <= amount_paid),
  constraint pos_transactions_refund_fields_together
    check (
      (refund_amount is null and refund_method is null and refunded_at is null)
      or (refund_amount is not null and refund_method is not null and refunded_at is not null)
    ),
  constraint pos_transactions_refunded_status_needs_refund
    check (status <> 'refunded' or refund_amount is not null)
);

comment on table pos_transactions is
  'One row per POS sale. Cash only for now. Amounts are in the row''s currency; never sum across currencies.';
comment on column pos_transactions.client_record_id is
  'Idempotency key set by the register''s offline queue. A retried send must not create a second row.';
comment on column pos_transactions.amount_paid is
  'What changed hands at this transaction: grand_total, or deposit_total for an inspection order.';
comment on column pos_transactions.change_given is
  'cash_received minus amount_paid. Enforced by check constraint for cash sales.';

create table if not exists pos_transaction_items (
  id              bigint generated always as identity primary key,
  transaction_id  uuid not null references pos_transactions (id) on delete cascade,
  line_no         smallint not null check (line_no > 0),

  -- Product identity is captured as text at the time of sale so a receipt
  -- still reads correctly after a rename or price change. A products table
  -- can be referenced from sku later without changing these rows.
  sku             text not null,
  product_name    text,
  category        text,                    -- men | women | ext | supply | service
  spec            text,                    -- colour / grey / length label as printed, e.g. '#1B Off black · 20% grey'
  grade           text not null default 'std'
                  check (grade in ('std', 'ob', 'os1', 'os2')),   -- standard, open box, off standard good / low

  quantity        integer not null check (quantity > 0),
  unit_price      numeric(12,2) not null check (unit_price >= 0),
  line_total      numeric(12,2) generated always as (round(unit_price * quantity, 2)) stored,
  made_to_order   boolean not null default false,

  unique (transaction_id, line_no)
);

comment on table pos_transaction_items is
  'Lines of a POS sale. unit_price is the price actually charged after any grade discount or option upcharge.';

create index if not exists pos_transactions_sold_at_idx      on pos_transactions (sold_at desc);
create index if not exists pos_transactions_location_day_idx on pos_transactions (location_id, sold_at desc);
create index if not exists pos_transactions_status_idx       on pos_transactions (status);
create index if not exists pos_transaction_items_sku_idx     on pos_transaction_items (sku);
create index if not exists pos_transaction_items_txn_idx     on pos_transaction_items (transaction_id);

-- Keep updated_at honest.
create or replace function pos_set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists pos_transactions_set_updated_at on pos_transactions;
create trigger pos_transactions_set_updated_at
  before update on pos_transactions
  for each row execute function pos_set_updated_at();

-- Cash drawer reconciliation: one line per till, per local day, per currency.
-- Refunds paid back in cash come off the drawer.
create or replace view pos_daily_cash_summary as
select
  location_id,
  currency,
  (sold_at at time zone 'America/Toronto')::date                     as sale_day,
  count(*)                                                           as sales,
  count(*) filter (where is_inspection)                              as inspection_deposits,
  sum(amount_paid)                                                   as amount_paid,
  sum(tax_total)                                                     as tax_total,
  sum(cash_received)                                                 as cash_received,
  sum(change_given)                                                  as change_given,
  sum(refund_amount) filter (where refund_method = 'cash')           as cash_refunded,
  sum(amount_paid)
    - coalesce(sum(refund_amount) filter (where refund_method = 'cash'), 0) as net_cash_in_drawer
from pos_transactions
where status <> 'void'
  and payment_method = 'cash'
group by location_id, currency, (sold_at at time zone 'America/Toronto')::date;

comment on view pos_daily_cash_summary is
  'Per till, per Toronto calendar day, per currency. Use for end-of-day cash counts.';

-- Row Level Security: on, with no policies. Only the service role (server-side
-- code holding the service key) can read or write until policies are added for
-- whatever auth the register ends up using. Default deny is the safe start.
alter table pos_transactions      enable row level security;
alter table pos_transaction_items enable row level security;
