# Google Ads API — Basic Access application, compliance follow-up answers

Written 2026-09-10 in reply to the Google Ads API compliance team's follow-up
on Superhairpieces' Basic Access application. The design document they
already hold is `google-ads-api-application/
Superhairpieces_Google_Ads_API_Design_Document.pdf` (v1.0, 2 September 2026);
the answers below are written to stay consistent with it. Sibling document
for the Amazon application: `amazon-spapi-application-answers.md`.

Google asked two things:

1. **Core business model** — the specific product or service, primary
   customers, and the "value exchange occurring on your platform".
2. **Functional API necessity** — which API features/services are critical,
   and how access improves or enables the service provided to users.

The wording of question 1 ("your platform") is a template aimed at SaaS
vendors. The reply therefore states plainly, and early, that Superhairpieces
is a retailer and the advertiser, and that the tool has no external users.

Fill the `[...]` placeholders before sending. See the notes at the end, and
read the **process note** — Google changed the application process on
9 September 2026 and this thread may be superseded.

---

## Reply (ready to paste)

**Subject:** Re: Google Ads API access application — Superhairpieces (additional information)

Hello,

Thank you for the follow-up. Our answers to both questions are below. In short: Superhairpieces is an online retailer of hairpieces, and we are the advertiser. The tool described in our design document is an internal reporting and campaign-management console for our own Google Ads accounts. It is not a platform, it has no external users, and it is not offered to anyone else.

**1. Core business model**

*What we sell.* Superhairpieces designs and sells non-surgical hair replacement products: men's hair systems (toupees), women's wigs and hair toppers, and the supplies needed to wear and maintain them (adhesives, tapes, solvents and hair-care products, including our own Super Tapes line and third-party brands such as Walker Tape and Professional Hair Labs). We also operate hair-replacement salons in the Greater Toronto Area that provide consultation, fitting and installation, and maintenance services for those products. The company employs over 500 people.

*Where we sell.* Six e-commerce storefronts, each a separate BigCommerce store serving one market: superhairpieces.com (United States), superhairpieces.ca (Canada), and superhairpieces.nl, .fr, .es and .de (Netherlands, France, Spain, Germany). [Across the six stores we ship roughly N orders a month.] Our Shopping campaigns are fed by our Google Merchant Center accounts 5298296396 (superhairpieces.ca) and 289630622 (superhairpieces.com and the European storefronts).

*Who our customers are.*

- Retail consumers experiencing hair loss or thinning: men and women in those six countries who buy a hair system, wig or topper and then the recurring supplies to maintain it. This is the large majority of our orders.
- Salons and independent stylists who fit hairpieces for their own clients. They register a professional account with a business registration or licence and buy from us at trade pricing (our "Tier 1" customer group, 10% below retail).
- Salon clients in the Greater Toronto Area who visit our locations for consultation, installation and maintenance appointments.

*The value exchange.* It is a conventional retail exchange. The customer pays us at checkout on one of our storefronts, or in person at a salon, and receives a physical product shipped from our warehouse or a service performed at our location. We earn a margin on goods and services. There is no platform in the software sense: we do not host other sellers, we do not sell software, data or advertising services, we do not manage advertising for anyone else, and nobody pays us for access to anything. Google Ads is a marketing expense for us. We are the advertiser, paying Google to bring buyers to our own storefronts.

**2. Functional API necessity**

*Why the API rather than the Google Ads interface.* We run [six] Google Ads accounts, one per storefront, in five languages and three currencies (CAD, USD, EUR), managed by a marketing team of two to three people. Today performance data is exported from each account's interface into spreadsheets and reconciled by hand against order data from our BigCommerce stores. That is slow and error-prone, and it means an under-performing campaign in, say, the Spanish account can run for days before anyone notices. We need to (a) pull the same performance data from all six accounts automatically into our own database, where it can be joined with data Google Ads does not have — order margin, refunds and returns, retail versus wholesale orders, and stock on hand — and (b) apply the resulting decisions back to the accounts in a controlled, logged way. Neither is possible through the interface alone, and the interface's automated rules and Google Ads Scripts operate inside a single account with no visibility of our order and inventory data.

*Which services are critical.*

Read (Phase 1, roughly 200 operations per day across six accounts):

- GoogleAdsService.SearchStream with GAQL, reading the campaign, campaign_budget, ad_group, ad_group_criterion / keyword_view, search_term_view, shopping_performance_view, asset_group, ad_group_ad and change_event resources with cost, impression, click, conversion, conversion-value, CTR and impression-share metrics. Results are stored in our private Postgres database on Google Cloud. The staff dashboard reads from that database, never from the API directly.
- The recommendation resource, so that Google's own suggestions appear beside our tool's proposals.

Write (Phase 2, fewer than 50 operations per day, every one approved by a staff member before it is sent):

- CampaignBudgetService.MutateCampaignBudgets: adjust a campaign's daily budget, capped at plus or minus 20% per day.
- CampaignService.MutateCampaigns and AdGroupService.MutateAdGroups: pause or re-enable a campaign or ad group after a sustained period below our return-on-ad-spend floor.
- CampaignCriterionService.MutateCampaignCriteria: add negative keywords identified from search terms with clicks and no conversions.
- AdGroupCriterionService.MutateAdGroupCriteria: pause under-performing keywords (pause only, never remove).
- CampaignService bidding-strategy fields: step changes to target ROAS or target CPA of at most 10%, no more than once every seven days.
- AdGroupAdService.MutateAdGroupAds: create draft responsive search ads in PAUSED status for staff to review and enable manually.

We do not need, and will not use, Customer Match or any other audience or user-list upload, offline conversion upload, account creation, user management, billing services, or access to any account we do not own.

*How this access improves the service we provide.*

- For our staff: one console covering all six markets, refreshed every six hours, with every recommendation tied to a stated reason and a before/after value. A change that today means signing in to six accounts and cross-checking a spreadsheet becomes a reviewed, logged, one-click approval.
- For our customers: advertising budget allocated on real margin and real stock, so we stop promoting products we cannot ship and put spend behind the markets and products where it converts; faster removal of irrelevant traffic (searches such as "free wig" or "wig sewing pattern"); and consistent ad quality across five languages from a small team. Customer acquisition cost feeds directly into what we can charge, so this supports our mission of delivering quality hairpieces at an affordable price.
- For Google: predictable, low-volume, policy-compliant API use from one private service, with every write operation logged with approver, timestamp, before/after values and request ID.

Our marketing team is the only user of the tool. Everyone signs in with a company Google Workspace account. There is no public sign-up, no customer-facing component, and no way for anyone outside Superhairpieces to connect a Google Ads account.

*Details for your review.*

- Google Ads manager account (MCC) ID: [xxx-xxx-xxxx]
- Google Cloud project holding the OAuth client: [project ID]
- API contact: manne@superhairpieces.com (monitored daily)
- Design document v1.0, 2 September 2026: re-attached

[Optional, if you have re-applied through Cloud Console by the time you send this: "Following the 9 September transition to Cloud-managed access, we have also submitted a Basic Access application from Google Cloud project [project ID]. If that application supersedes this thread, please let us know."]

Thank you again for your help. We are happy to provide a screen-share walkthrough of the tool or anything else you need.

Manne Tsang
Head of Digital Transformation, Superhairpieces
superhairpieces.com

---

## Before sending

- **Fill the placeholders:** number of Google Ads accounts (the design
  document says six, one per storefront; correct both if the real count is
  different), monthly order volume (optional sentence; delete it if you
  would rather not share a figure), the manager account ID, and the Cloud
  project that holds the OAuth client.
- **Re-attach the design document PDF.** The reply refers to it.
- **"Over 500 people"** comes from the company background; confirm it is
  the figure you want on record with Google.
- **Merchant Center IDs** are included because they are the most verifiable
  proof that we are a merchant. Drop the line if you prefer not to share
  them. Gen'C Beauty's account (670525760) is deliberately not mentioned; it
  is a different business.
- Nothing in the reply goes beyond what the design document already
  promised (services, guardrails, volumes, phases). If the tool's scope has
  changed since 2 September, change both.

## Process note (verified 2026-09-10 on developers.google.com)

Google moved developer-token sign-up and API-access applications out of the
Google Ads manager account's API Center and into the Google Cloud Console on
9 September 2026. Quotes from the developer-token policy page
(developers.google.com/google-ads/api/docs/api-policy/developer-token):

- "Developer tokens were sunset on September 9, 2026."
- "Don't sign up for a developer token from the API Center page or attempt
  to apply for API access from the API Center page in your Google Ads
  manager account as these processes have been transitioned to Google Cloud
  Console."
- "Any pending Basic Access applications that you initiated from your Google
  Ads manager account's API Center prior to September 9, 2026 will be
  closed." ... "You should re-apply for Basic Access from your Google Cloud
  project's Google Ads API Overview page."
- "Basic Access applications are now automated and will be reviewed within
  minutes after brand verification." Brand verification is required for
  Basic and Standard Access. "If you need Standard Access, you need a manual
  review."

What this means for us:

1. **Send the reply above anyway.** A human reviewer asked; answering keeps
   that thread alive and costs nothing.
2. **In parallel, re-apply from the Cloud project.** Open the Google Ads
   API Overview page in the Cloud Console for the project that holds the
   OAuth client, complete brand verification (steps below), then submit the
   Basic Access application there. That path is automated and may approve
   within minutes, independent of the email thread.
3. If Google emails that the API Center application was closed, that is the
   transition, not a rejection. Re-apply as in step 2 and reuse the text
   above where the form asks about the business or the tool.

### Brand verification steps

From developers.google.com/google-ads/api/docs/api-policy/brand-verification,
read 2026-09-10. Do this in the Cloud project that holds the OAuth client the
tool will authenticate with.

1. Cloud Console → select the project → **APIs & Services → OAuth consent
   screen**.
2. **Overview** tab: fill in the details and click Create (skip if the
   consent screen already exists).
3. **Audience** tab: for a Google Workspace organisation, click **Make
   external**, set Publishing status to **In production**, and confirm
   "Push to Production?". Google's page is explicit that for the Basic
   Access review the user type must be External and the status In
   production, even for an internal-use tool. "External" here classifies
   the OAuth consent screen only; who can sign in to the tool is still
   controlled by the tool itself.
4. **Branding** tab: fill in all branding information (app name, support
   email, logo, authorised domains, privacy-policy link) and Save.
5. Click **Verify Branding** (top right of the Branding page). It takes a
   few minutes; the page lists any errors and how to fix them.
6. When it succeeds, click **Publish branding**. The project is now brand
   verified.
7. Return to the **Google Ads API Overview page** in the same project and
   submit the Basic Access application. Google says these are now reviewed
   automatically within minutes.
