-- Remove tax: 0004
--
-- The register sells at the shelf price with no tax handling at all, so the
-- tax columns, the tax-inclusive flag and the tax terms in the totals rule go.
-- What the customer pays is subtotal minus discount.

drop view if exists pos_daily_cash_summary;

alter table pos_transactions drop constraint if exists pos_transactions_totals_add_up;
alter table pos_transactions
  drop column if exists tax_rate,
  drop column if exists tax_label,
  drop column if exists tax_total,
  drop column if exists prices_include_tax;
alter table pos_transactions add constraint pos_transactions_totals_add_up
  check (grand_total = round(subtotal - discount_total, 2));

alter table pos_products drop column if exists price_includes_tax;
comment on column pos_products.price is
  'Customer-facing price in `currency`, exactly as charged. A null price means the item cannot be rung up.';

create or replace view pos_daily_cash_summary as
select
  location_id,
  currency,
  (sold_at at time zone 'America/Toronto')::date                     as sale_day,
  count(*)                                                           as sales,
  count(*) filter (where is_inspection)                              as inspection_deposits,
  sum(amount_paid)                                                   as amount_paid,
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
