# Google Ads API — Basic Access application, compliance follow-up answers (Gen'C Beauty)

Written 2026-09-10, rewritten 2026-09-15 once it was confirmed that the Google
Ads account on this application belongs to **Gen'C Beauty (gencbeauty.com)**.
The design document originally submitted (`google-ads-api-application/
Superhairpieces_Google_Ads_API_Design_Document.pdf`, v1.0, 2 September 2026)
described the tool for Superhairpieces, which is the likely reason Google
asked for "a clearer picture of your business model". The corrected document
is `google-ads-api-application/GenC_Beauty_Google_Ads_API_Design_Document.pdf`
(v2.0, 15 September 2026, built by `build_design_doc_gencbeauty.py`); attach
it to the reply. Same tool, same API services and guardrails; company,
channels, users and mockups rewritten.

The **9 September 2026 process change** (Google closed pending API Center
applications and moved Basic Access to the Cloud Console with brand
verification) is documented in `google-ads-api-application-answers.md` and
applies here too: send this reply, and in parallel re-apply from the Cloud
project's Google Ads API Overview page after brand verification.

Fill the `[...]` placeholders before sending and read the notes at the end.

---

## Reply (ready to paste)

**Subject:** Re: Google Ads API access application — Gen'C Beauty (additional information)

Hello,

Thank you for the follow-up. Our answers to both questions are below. In short: Gen'C Beauty is an online retailer of beauty and salon-professional products, and we are the advertiser. The tool described in our design document is an internal reporting and campaign-management console for our own Google Ads account. It is not a platform, it has no external users, and it is not offered to anyone else.

We have also attached an updated design document (version 2.0). The version 1.0 document submitted with the application described the tool in the context of Superhairpieces, our sister company, which shares our e-commerce team and the internal platform the tool runs on. Version 2.0 describes the same tool for the Gen'C Beauty account this application concerns. The API services, guardrails and data handling are unchanged.

**1. Core business model**

*What we sell.* Gen'C Beauty is a first-party retailer and authorised reseller of beauty and personal-care products, and the owner of our own Gen'C Beauty brand. Our range is Korean skincare (brands such as Beauty of Joseon, Anua, SOME BY MI, TIRTIR, SKIN1004, Round Lab, Torriden, I'm From and VT Cosmetics), hair care and textured-hair products (Mielle, Adore, Luster's, ORS, Shea Moisture), cosmetics, and salon-professional supplies and equipment: wax and depilatory products, disposables such as table sheets and neck strips, professional nail equipment, tools and massage tables. We hold physical stock: our inventory system tracks roughly 10,600 products and 2,800 kits across our warehouses in the Greater Toronto Area, Canada, and we ship orders ourselves or through marketplace fulfilment programmes.

*Where we sell.* Our own storefront, gencbeauty.com, runs on BigCommerce and is the only property we advertise with Google Ads. Its Shopping campaigns are fed by our Google Merchant Center account 670525760. We also sell the same catalogue on Amazon (about 5,100 SKUs, mostly Fulfilled by Amazon) and on Walmart Canada (Walmart Fulfillment Services); advertising on those marketplaces runs on their own advertising platforms, not on Google Ads. [Across all channels we ship roughly N orders a month.]

*Who our customers are.*

- Retail consumers in Canada and the United States buying skincare, hair care and cosmetics for personal use. They find us through search and Shopping ads for specific products and brands, and many reorder consumables such as serums, masks and treatments.
- Salon, spa and nail professionals buying supplies and equipment for their businesses: wax, disposables, nail and esthetics equipment, tools and furniture. These are repeat, higher-volume orders.

*The value exchange.* It is a conventional retail exchange. The customer pays at checkout and receives physical goods shipped from our warehouse, or by Amazon or Walmart for orders placed on those marketplaces. We earn a margin between what we pay the brand or distributor and what the customer pays us. There is no platform in the software sense: we do not host other sellers, we do not sell software, data or advertising services, we do not manage advertising for anyone else, and nobody pays us for access to anything. Google Ads is a marketing expense for us. We are the advertiser, paying Google to bring buyers to gencbeauty.com.

**2. Functional API necessity**

*Why the API rather than the Google Ads interface.* We already operate one internal operations dashboard for our own selling accounts. It integrates the Walmart Marketplace API, the Amazon Selling Partner API and our inventory system (SkuVault), and it computes per-SKU profit using actual marketplace fees, promotional discounts and cost of goods, alongside live stock per SKU across our warehouses and the marketplace fulfilment centres. Google Ads is the one sales channel whose cost data is not in that system. Today its performance data is exported from the Google Ads interface into spreadsheets and matched to gencbeauty.com orders by hand. With a catalogue of more than 10,000 low-unit-price SKUs, hand matching cannot tell us which products are profitable to advertise, which are being advertised while out of stock, and how the storefront's true return compares with Amazon and Walmart. We need to (a) pull campaign, keyword, search-term and product-level performance from our Google Ads account automatically into the same database, where it joins the margin and stock data Google Ads does not have, and (b) apply the resulting decisions back to the account in a controlled, logged way. Neither is possible through the interface alone, and the interface's automated rules and Google Ads Scripts operate inside the Google Ads account with no visibility of our margin or inventory data.

*Which services are critical.*

Read (Phase 1, roughly 30 to 70 operations per day):

- GoogleAdsService.SearchStream with GAQL, reading the campaign, campaign_budget, ad_group, ad_group_criterion / keyword_view, search_term_view, shopping_performance_view (segmented by product item ID, which is how we join to our SKUs), asset_group, ad_group_ad and change_event resources with cost, impression, click, conversion, conversion-value, CTR and impression-share metrics. Results are stored in our private Postgres database. The staff dashboard reads from that database, never from the API directly.
- The recommendation resource, so that Google's own suggestions appear beside our tool's proposals.

Write (Phase 2, fewer than 50 operations per day, every one approved by a staff member before it is sent):

- CampaignBudgetService.MutateCampaignBudgets: adjust a campaign's daily budget, capped at plus or minus 20% per day.
- CampaignService.MutateCampaigns and AdGroupService.MutateAdGroups: pause or re-enable a campaign or ad group after a sustained period below our return-on-ad-spend floor, or while every product it promotes is out of stock.
- CampaignCriterionService.MutateCampaignCriteria: add negative keywords identified from search terms with clicks and no conversions.
- AdGroupCriterionService.MutateAdGroupCriteria: pause under-performing keywords (pause only, never remove).
- CampaignService bidding-strategy fields: step changes to target ROAS or target CPA of at most 10%, no more than once every seven days.
- AdGroupAdService.MutateAdGroupAds: create draft responsive search ads in PAUSED status for staff to review and enable manually.

We do not need, and will not use, Customer Match or any other audience or user-list upload, offline conversion upload, account creation, user management, billing services, or access to any account we do not own.

*How this access improves the service we provide.*

- For our staff: Google Ads becomes one more tab in the dashboard the team already uses for Amazon, Walmart and inventory, refreshed every six hours, with every recommendation tied to a stated reason and a before/after value. A change that today means exporting a report and cross-checking a spreadsheet becomes a reviewed, logged, one-click approval.
- For our customers: advertising spend allocated on real margin and real stock, so we stop promoting products we cannot ship, put budget behind the products and brands that convert, and remove irrelevant traffic quickly. Lower acquisition cost is what lets us keep prices on gencbeauty.com competitive with the marketplaces.
- For Google: predictable, low-volume, policy-compliant API use from one private service, with every write operation logged with approver, timestamp, before/after values and request ID.

Our e-commerce team is the only user of the tool. Everyone signs in with a company Google Workspace account. There is no public sign-up, no customer-facing component, and no way for anyone outside the company to connect a Google Ads account.

*Details for your review.*

- Google Ads account ID: [xxx-xxx-xxxx] [under manager account xxx-xxx-xxxx, if applicable]
- Google Cloud project holding the OAuth client: [project ID]
- API contact: manne@superhairpieces.com (monitored daily; a Superhairpieces address because the two companies share one e-commerce team)
- Design document version 2.0, 15 September 2026: attached

[Optional, if you have re-applied through Cloud Console by the time you send this: "Following the 9 September transition to Cloud-managed access, we have also submitted a Basic Access application from Google Cloud project [project ID]. If that application supersedes this thread, please let us know."]

Thank you again for your help. We are happy to provide a screen-share walkthrough of the tool or anything else you need.

Manne Tsang
Head of Digital Transformation, Gen'C Beauty
gencbeauty.com

---

## Before sending

- **Attach the v2.0 design document**, not the Superhairpieces v1.0. The
  reply and the document both say v2.0 supersedes v1.0 and explain why; if
  v1.0 was in fact never sent to Google, delete the second paragraph of the
  reply and the "Document history" paragraph in the PDF (section 1) and
  rebuild.
- **Fill the placeholders:** account ID and whether a manager account exists,
  the Cloud project, and the optional monthly order volume.
- **Sister-company wording.** The reply, the PDF and the API-contact line all
  describe Superhairpieces as a sister company sharing one e-commerce team.
  Change the wording if that is not how the relationship should be described
  to Google.
- **Title and contact.** Both documents use "Head of Digital Transformation,
  Gen'C Beauty" and manne@superhairpieces.com. Change them if the contact on
  file with Google is different.
- **Verify the stated scale.** "Roughly 10,600 products and 2,800 kits" and
  "about 5,100 Amazon SKUs" are the 2026-08-19 figures from the Amazon
  application; "more than 10,000 SKUs" follows from them.
- **Google Ads usage claims.** The reply says Google Ads advertises only
  gencbeauty.com, that Shopping runs through Merchant Center 670525760, and
  that the targets are Canada and the United States. Confirm all three; the
  storefront shows USD pricing, so check the account currency too.
- **Consistency.** Services, guardrails (20% budget cap, 10% bid-target
  steps, pause-never-remove, ads created paused), phases and the
  approval-before-write rule are identical in the reply, the v2.0 PDF and
  the v1.0 PDF. The only additions are the out-of-stock pause reason and the
  per-product Shopping segmentation, both within the same services.
