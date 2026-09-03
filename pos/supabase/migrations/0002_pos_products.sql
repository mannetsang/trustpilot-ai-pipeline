-- POS products: 0002
--
-- The product list for the register, imported from the show-prep Google Sheet
-- by pos/import_products.py. One row per SKU; barcodes live in their own table
-- because several SKUs carry two barcodes (a UPC and an Amazon ASIN, or two
-- UPCs for one product).
--
-- Show prices are whole dollars with tax already included (the sheet's website
-- price is exactly the show price / 1.13), so this migration also teaches
-- pos_transactions about tax-inclusive pricing.

create table if not exists pos_products (
  id                  uuid primary key default gen_random_uuid(),
  sku                 text not null unique,
  name                text not null,
  category            text not null,                -- sheet 类别: CN, KR, US, Clearance

  -- Selling price as shown to the customer. price_includes_tax says whether the
  -- register must add tax on top (false) or back it out for the receipt (true).
  price               numeric(12,2) check (price is null or price >= 0),
  price_includes_tax  boolean not null default true,
  currency            char(3) not null default 'CAD' check (currency in ('CAD', 'USD', 'EUR')),
  cost                numeric(12,2) check (cost is null or cost >= 0),

  -- Stock brought to the show, as counted (sheet 本次展会拿货数量). show_qty is
  -- the parsed number; show_qty_raw keeps markers such as '2+1*' or '*'.
  show_qty            integer,
  show_qty_raw        text,

  is_set              boolean not null default false,   -- sheet Note = 'Set'
  is_clearance        boolean not null default false,   -- sheet 清仓 = TRUE
  is_active           boolean not null default true,    -- sellable at the register
  note                text,

  -- Provenance: the raw sheet row and where it came from.
  sheet               jsonb,
  sheet_row           integer,
  imported_at         timestamptz,

  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

comment on table pos_products is
  'Register product list, one row per SKU, imported from the show-prep sheet. Barcodes are in pos_product_barcodes.';
comment on column pos_products.price is
  'Customer-facing price in `currency`. Tax-inclusive when price_includes_tax; a null price means the item cannot be rung up.';

create index if not exists pos_products_category_idx on pos_products (category);
create index if not exists pos_products_name_idx     on pos_products (lower(name));

create table if not exists pos_product_barcodes (
  barcode     text primary key,                            -- UPC, EAN or ASIN as scanned
  product_id  uuid not null references pos_products (id) on delete cascade,
  created_at  timestamptz not null default now()
);

comment on table pos_product_barcodes is
  'Scannable codes. Many codes may point at one product; one code points at exactly one product.';

create index if not exists pos_product_barcodes_product_idx on pos_product_barcodes (product_id);

drop trigger if exists pos_products_set_updated_at on pos_products;
create trigger pos_products_set_updated_at
  before update on pos_products
  for each row execute function pos_set_updated_at();

-- Sale lines can now point at the product they came from. sku stays as the
-- text captured at the time of sale, so a receipt survives a product rename.
alter table pos_transaction_items
  add column if not exists product_id uuid references pos_products (id),
  add column if not exists barcode    text;

create index if not exists pos_transaction_items_product_idx on pos_transaction_items (product_id);

-- Tax-inclusive sales. When prices_include_tax is true the line prices and
-- subtotal already contain tax; tax_total is the amount backed out for the
-- receipt and does not add to the total.
alter table pos_transactions
  add column if not exists prices_include_tax boolean not null default false;

alter table pos_transactions drop constraint if exists pos_transactions_totals_add_up;
alter table pos_transactions add constraint pos_transactions_totals_add_up check (
  (not prices_include_tax and grand_total = round(subtotal - discount_total + tax_total, 2))
  or (prices_include_tax and grand_total = round(subtotal - discount_total, 2) and tax_total <= grand_total)
);

alter table pos_products         enable row level security;
alter table pos_product_barcodes enable row level security;
