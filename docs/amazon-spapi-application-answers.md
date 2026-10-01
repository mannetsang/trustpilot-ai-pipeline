# Amazon Selling Partner API — application answers

Written 2026-08-19 for the Gen'C Beauty seller account (session `3eaf15c5`).
Reused for later applications: swap the business description and figures for
the account being applied for, keep the structure. Question 1 has a
**500-word** limit (not characters).

## Q1. Explain your primary business activity on Amazon and how your business will utilize Selling Partner API in its operations. (491 words)

**PRIMARY BUSINESS ACTIVITY**

Gen'C Beauty is a first-party retailer and authorised reseller of beauty, personal-care and salon-professional goods, and the owner of our own Gen'C Beauty brand. We sell Korean skincare (Beauty of Joseon, Anua, SKIN1004, Round Lab, Torriden), hair care and texture products (Mielle, Adore, Luster's, ORS, Shea Moisture), and salon supplies and equipment including wax, adhesives, tools and massage tables.

We currently maintain roughly 5,100 Amazon SKUs mapped to about 4,970 ASINs, fulfilled primarily through Fulfillment by Amazon from our own warehouse in the Greater Toronto Area, with a portion merchant-fulfilled. We also run a parallel Walmart Marketplace business using Walmart Fulfillment Services.

Our model is inventory-led. We hold and manage physical stock in SkuVault, our inventory system of record: 10,656 products and 2,806 kits, with per-bin locations across four warehouses. We replenish Amazon by building inbound FBA shipments from that stock. In the past week alone we raised 13 inbound documents covering 405 SKU lines and 8,449 units.

**HOW WE WILL USE THE SELLING PARTNER API**

We operate one internal operations dashboard for our own seller accounts. It already integrates Walmart's Marketplace API end to end (orders, inventory, listings, inbound shipments, fees) and SkuVault's API. Amazon is the one major channel with no API connection, so every Amazon figure in the system is manual, stale or absent. Two concrete symptoms: all 5,116 of our Amazon listing records still read status "Inactive" because nothing can refresh them, and SkuVault reports zero FBA units for every SKU because we cannot read Amazon's inventory position.

We will use the Selling Partner API to close those gaps:

Orders and inventory: retrieve orders and order items to reconcile Amazon sales against our own ledger, and read FBA inventory so SkuVault reflects Amazon-held stock. This drives per-SKU velocity and replenishment decisions that are currently guesswork.

Inbound shipments: create and track the FBA shipments we build by hand today, and reconcile received against shipped quantities per SKU so short receipts are detected on arrival rather than months later.

Listings and pricing: keep listing status, ASIN mapping and attributes current across our catalogue, list products we already stock, and monitor our prices and Buy Box position so pricing stays consistent across Amazon, Walmart and our storefront.

Finance and reporting: retrieve fees and settlement data, and schedule inventory, sales and returns reports, so per-SKU profit and margin use actual Amazon charges rather than estimates.

Account health: surface performance and policy information in the dashboard the team already watches, instead of a separate Seller Central login.

Buyer contact: send Amazon's standard review request for delivered orders, and respond to buyer messages about our own orders. Buyer data is used only to resolve the order it relates to, never for marketing, and is not exported.

All data is used solely for our own internal operations. The dashboard is private, password-protected and used only by our own staff. We do not resell, share or expose Amazon data to third parties.

> Check before pasting: "with a portion merchant-fulfilled" was written because the
> Amazon Logistics role was requested. If the account is FBA-only, delete that clause
> and drop the Amazon Logistics role so the two answers do not contradict.

## Q2. Describe the application or feature(s) your organization intends to build using the functionality in the requested roles.

**Application overview**

We operate a single internal operations dashboard for our own Amazon seller accounts. It already integrates Walmart Marketplace and SkuVault (our inventory system of record — 10,656 products, 2,806 kits, per-bin locations across four warehouses) and this application adds Amazon as the remaining channel. It is private, password-protected, used only by our own staff, and no Amazon data is resold, shared or exposed to third parties.

**Inventory and Order Tracking** — The core feature. We reconcile Amazon orders and order items against our own inventory ledger, and read FBA inventory summaries so SkuVault reflects Amazon-held stock. Today SkuVault reports zero FBA units for every SKU because we have no way to pull that position, which means replenishment is guesswork. This also drives per-SKU sales velocity over 7/30/90-day windows, feeding the same replenishment view we already run for Walmart.

**Amazon Fulfillment** — We build inbound FBA shipments from warehouse pick documents; in the last week alone that was 13 documents, 405 SKU lines and 8,449 units, all created by hand. We will create and track those shipments through the API and reconcile received against shipped quantities per SKU, so short receipts are detected on arrival instead of months later.

**Product Listing** — Maintain our own listings: keep status, ASIN mapping, titles and identifiers current across roughly 5,100 SKUs and 4,970 ASINs, correct missing or wrong attributes, and create listings for new products we already stock. Every one of our 5,116 Amazon listing records currently reads status "Inactive" because nothing can refresh it.

**Pricing** — Monitor our own prices and Buy Box position and keep pricing consistent across Amazon, Walmart and our BigCommerce storefront, which currently drift independently.

**Finance and Accounting** — Retrieve fees and settlement data so per-SKU profit and margin include real referral and fulfilment costs. We compute this for Walmart today from its published fee schedule; on Amazon we would use actual charges.

**Selling Partner Insights** — Read account and performance information so account health and policy issues surface in the same dashboard the team already watches, rather than requiring a separate Seller Central login.

**Brand Analytics** — We own and sell our own brand (Gen'C Béauty). Where brand-registered, we would use search and sales data to guide which of our SKUs to stock and list.

**Buyer Solicitation** — Send Amazon's standard review request for delivered orders, through Amazon's own permitted mechanism, on our own orders only.

**Buyer Communication** — Read and respond to buyer messages relating to our own orders, so order issues are handled alongside the order record. We do not export, store beyond operational need, or use buyer data for marketing.

**Amazon Logistics** — Purchase Amazon shipping for merchant-fulfilled orders where that is the cheaper route than shipping ourselves.

**Amazon Warehousing and Distribution** — Read AWD shipment and inventory detail so upstream AWD stock appears in the same inventory picture as FBA and our own warehouses.

**Sustainability Certification** — Submit sustainability and compliance certifications for our beauty and personal-care products where required.

## Q3. Describe why your organization requires Restricted roles containing Personally Identifiable Information to build your application or feature. Include details for all Restricted roles you are applying for. (470 words)

Written 2026-09-09 for the second seller account. Amazon's restricted roles are
Direct-to-Consumer Shipping, Tax Invoicing, Tax Remittance and Professional
Services (Buyer Communication is **unrestricted**, contrary to the 2026-08-19
note). Delete any section for a role not requested.

**WHY WE REQUIRE RESTRICTED ROLES**

This application is a private, internal operations tool for our own Amazon seller account; it is not offered to any other seller. The same software already runs in production against our first seller account. Most of what it does (FBA inventory and replenishment, listings, pricing, fees, reporting) needs no buyer data, and most of our orders are fulfilled by Amazon, where we never need the buyer's details. We request Restricted roles only for the one part of the operation that cannot run without them: orders we ship ourselves from our own warehouse, and the sales tax we remit on them.

**Direct-to-Consumer Shipping**

A portion of our orders is merchant-fulfilled from our warehouse in the Greater Toronto Area. Our inventory system, SkuVault, already receives every merchant-fulfilled order from this application every ten minutes so stock is allocated and picked. What it cannot receive is the ship-to address, because getOrders and the all-orders report return it redacted. Warehouse staff therefore re-key the address from Seller Central onto the pick slip and carrier label, which is slow and causes mis-shipments. With this role we will call getOrderAddress under a Restricted Data Token scoped to the individual order, pass the recipient name, address and, where the carrier requires it, phone number once into the pick document and shipping label, and use the Merchant Fulfillment API to buy Amazon shipping where it is cheaper. This data is retained only until the shipment is confirmed delivered, and no longer than 30 days, then deleted automatically.

**Tax Remittance**

We are a GST/HST-registered Canadian business selling on Amazon.ca. Amazon collects and remits GST/HST only on behalf of sellers who are not registered; as a registered seller we collect it through Amazon's tax calculation service and remit it ourselves to the Canada Revenue Agency at the rate of the destination province (5%, 13% or 15%). To file correctly we need, per order, the ship-to province and postal code and the tax collected, which the sales tax report provides only under this role. We use only the destination jurisdiction and tax amounts; the buyer's name and street address in that report are not stored. Jurisdiction-level tax records are kept for the six years the Canada Revenue Agency requires; nothing else from the report is retained.

**How the data is protected**

Restricted data is requested only for the specific operation and order that needs it, transmitted over TLS, encrypted at rest, and held separately from our operational tables. Access is limited to named warehouse and finance staff behind authentication, and every access is logged. It is never written to application logs, exports, notifications or spreadsheets, never used for marketing or profiling, and never shared with anyone other than the carrier delivering the parcel. Deletion on the schedules above is automated. We have read and will comply with Amazon's Acceptable Use Policy and Data Protection Policy.

Optional paragraph, only if Tax Invoicing was ticked (insert before the protection section):

> **Tax Invoicing** — Business customers on Amazon.ca request GST/HST invoices for their purchases. To issue a compliant invoice we need the buyer's name and billing address and, for business buyers, their business name and tax registration number, retrieved with getOrderBuyerInfo for that order only. The invoice is generated once and delivered through Amazon's invoice upload; the buyer details are removed from our system after 30 days and only the invoice itself is kept for the statutory period.

Caveats: Tax Remittance only holds if the account is GST/HST registered and
self-remitting on .ca. The 30-day purge, separate storage, access logging and
no-PII-in-logs commitments must be built before restricted data is pulled.

## Notes from the original review

- Do **not** request Account Information Service Provider or Payment Initiation Service Provider. They are PSD2 Open Banking roles for licensed European Third Party Providers, and the "orgID issued by the national authority" field only exists for them. Leave both unchecked and the orgID field blank.
- Buyer Communication is the only role touching buyer PII and triggers the extra data-protection questionnaire. Drop it if the dashboard will not answer buyer messages.
- Brand Analytics requires Brand Registry. AWD only if the account actually uses Amazon Warehousing and Distribution.
