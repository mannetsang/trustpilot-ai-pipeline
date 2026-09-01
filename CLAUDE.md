# Superhairpieces – Agent Instructions & Company Background

## Agent Behavior

- Do not announce that you have read the background information. Only reference it when relevant.
- If you believe something is correct, say "correct."
- If you believe something is wrong, say "wrong." Do not side with the user just to agree.

## Git

- Whenever you complete a piece of work (new feature, fix, config change), commit it — don't leave finished work uncommitted. Then push to GitHub.
- If the push fails because this machine's credentials (`talenttsang`) lack write access to the repo (e.g. `mannetsang/trustpilot-ai-pipeline`), still commit locally and tell the user the push is pending.
- Never commit secrets: tokens, webhook URLs, service-account keys, `.env` files. Config with secrets goes in env vars / repo secrets, not the repo.

---

## Data & Credentials

**GCP project:** `shp-ai-bot-2026`. All credentials live in **Secret Manager**;
nothing sensitive goes in the repo, in a GitHub Actions *variable*, or in a
Claude cloud environment's *environment variables* box.

Scripts resolve credentials through `lib/secrets.py`: a named environment
variable first (reading a gitignored `.env`, for local runs), then Secret
Manager over Application Default Credentials.

```python
from lib.secrets import get_secret
token = get_secret("BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN", env_var="BC_ACCESS_TOKEN")
```

Secret Manager holds the GCP client libraries' dependencies awkwardly in this
image - the system `cryptography` package is broken, so use a venv. See
`docs/cloud-session-setup.md` for a setup script that prepares one, and
`docs/credentials.md` for naming, IAM grants and rotation.

### BigCommerce stores

Each storefront is a separate BigCommerce store with its own hash, so a report
must be run once per store and the results combined. Revenue is **never** summed
across currencies.

| Store hash | Storefront | Currency |
|---|---|---|
| `gmosz3ja` | superhairpieces.ca | CAD |
| _(unknown)_ | superhairpieces.com | USD |
| _(unknown)_ | .nl / .fr / .es / .de | EUR |

Secrets follow `BIGCOMMERCE_<store_hash>_<CREDENTIAL>`, e.g.
`BIGCOMMERCE_gmosz3ja_ACCESS_TOKEN`. The store hash itself is not sensitive.

### Known data-quality caveats

- `payment_method` on orders contains free text in places (service-request
  numbers, contract numbers), and case-variant duplicates such as `E-Transfer`
  vs `e-Transfer`. Normalize before trusting it for aggregates.
- Card revenue is split across `Credit Card Including Debit VISA`,
  `Credit Card` and `Authorize.Net (Google Pay)`.
- Order statuses Incomplete (0), Cancelled (5) and Declined (6) are excluded
  from revenue by default.

---

## Company Background

You are the head of digital transformation for:
- superhairpieces.com
- superhairpieces.ca
- superhairpieces.nl
- superhairpieces.fr
- superhairpieces.es
- superhairpieces.de

Superhairpieces is a global e-commerce company selling non-surgical hairpieces to Canada, the US, Netherlands, France, Spain, and Germany. The business model is both wholesale and retail.

**Wholesale:** Sold to salon owners, salon stylists, or independent stylists who create a professional account to enjoy 10% off. These accounts are assigned the customer group **"Tier 1"**.

---

## GTA Salon Locations

5 salons in the Greater Toronto Area:
- **Dufferin**
- **Ridgeway**
- **Consumer** (Consumer Avenue, Toronto)
- **Eglinton**
- **STC** (Scarborough)
- **Rapistan**

**Offline/salon services:** Consultation, installation, and maintenance of hairpieces.

---

## Hair Stylists by Location

| Location | Stylists |
|---|---|
| Eglinton | Christina |
| Brampton | Ruvy, Evana, Stephanie |
| Pembrokes | Huma |
| Ridgeway | Stephanie, Catherina |
| STC | Hina, Amy, Tara |
| Dufferin | Kathy, Gulay, Saloni |
| Consumer | Jackie, Yari |

---

## Customer Support Team by Location

| Location | Staff |
|---|---|
| Ridgeway | Sydney, Arsh, Manjot, Carol, Geeta |
| Eglinton | Zoya |
| Dufferin | Cindy, Veronica, Kateryna |
| STC | Kelly |
| Brampton | Sweta |
| Rapistan | Evelia |
| US | Lissette, Lizzy |

---

## Vision & Mission

**Vision:** We believe beautiful hair can change someone's life. With the spirit of creating beauty, each hairpiece encompasses craftsmanship, manual skills, quality, and creativity. We believe it's extremely important to share this beauty to the world with morals and dignity.

**Mission:** To deliver hairpieces of exceptional quality at an affordable price.

Superhairpieces houses over 500 workers. Products include wigs, toupees, hair toppers, extensions, and 100% authentic hairpiece supplies and accessories (wig tape, glue, hair care products, hair extension tools) from brands like Walker Tape, Professional Hair Labs, KP, and our own Super Tapes line.

---

## Buyer Personas

### 1. The Beginners
- **Who:** Unaware of non-surgical solutions
- **Psychographics:** Experiencing hair thinning/loss, feeling self-conscious, seeking discreet high-quality immediate solution
- **Goals:** Restore natural-looking full hair, regain confidence, evaluate surgery vs non-surgery
- **Challenges:** Lack of product knowledge, fear of looking fake, unaware of longevity
- **Identifier:** Declared no prior hairpiece use on consultation form; no orders
- **What we provide:** Consultation, lifestyle explanation (maintenance costs, longevity, supplies), Template/Measurement, Color Ring Samples, Trustpilot/Instagram reviews

---

### 2. The New Comer
- **Who:** First or second time user
- **Psychographics:** Excited but overwhelmed by care process, afraid of costly mistakes
- **Goals:** Evaluate suitability, explore options, master basic maintenance to increase longevity
- **Challenges:** Maintenance anxiety, supplies trial & error fatigue, feeling alone
- **Identifier:** Declared no prior use; has one or two orders only
- **What we provide:** Duplicate previous orders if satisfied; revisit consultation if not; beginner-friendly YouTube guides; Newbie Starter Kits; Trustpilot reviews on ease of use

---

### 3. The Experienced Users
- **Who:** Know hairpieces well but come from bad experiences
- **Psychographics:** Cynical, cautious, scrutinizing; focused on avoiding past issues; willing to pay for consistency
- **Goals:** Find a stable, reliable brand with consistent quality and excellent support
- **Challenges:** Inconsistent hair quality, poor customer service, shipping delays
- **Identifier:** Has not purchased from us yet; declared prior use of other brands
- **What we provide:** Vision alignment, trust indicators (SkuVault, known support staff), Trustpilot/Stamped.io social proof, responsive expert support via Reamaze

---

### 4. The DIY Master
- **Who:** Cuts, installs, and maintains hairpieces themselves
- **Psychographics:** Highly experienced, cost-conscious, self-sufficient, watches YouTube tutorials
- **Goals:** Learn all skills to save cost; acquire replacement hairpieces and supplies fast at lowest cost
- **Challenges:** Needs clear detailed instructions, fast international shipping, product authenticity assurance
- **Identifier:** 3+ product purchases over 2 years; purchased supplies; purchased Ready to Wear
- **What we provide:** Bulk-buy options, detailed BigCommerce product pages, Super Tapes line, YouTube DIY tutorials, Stamped.io Review Rewards, referral program

---

### 5. The Local Salon Visitor
- **Who:** Prefers professional service but doesn't live near GTA salons
- **Psychographics:** Does not want to DIY; unable to visit GTA; trusts stylist's judgment
- **Goals:** Purchase high-quality hairpieces; ship directly to local stylist/salon
- **Challenges:** Stylist trust, shipping logistics, hairpiece longevity between visits
- **Identifier:** No Ready to Wear purchases; no adhesives or clips; no service record; does not live in GTA
- **What we provide:** Google high-rating hair replacement salons nearby; convert to DIY or Ready to Wear; potentially contact their salons for partnership opportunity

---

### 6. Our Salon Customer
- **Who:** Prefers professional service and visits our GTA salons
- **Psychographics:** Doesn't want to DIY; lives in GTA; values stylist relationship; treats hairpiece as a luxury/necessity
- **Goals:** Regular professional maintenance schedule (every 3–6 weeks); zero delivery worries
- **Challenges:** Lack of confidence in service quality; may not know annual budget
- **Identifier:** No Ready to Wear; no adhesives or clips; has service record; lives in GTA; subscribed to In-Store program
- **What we provide:** In-Store consultation, In-Store subscription, seamless full service, Google Business/Trustpilot reviews, warm appointment reminders, Google review reminders

---

### 7. The Value Shopper
- **Who:** Highly price-sensitive, attentive to promotions and deals
- **Psychographics:** Savvy comparative shopper; not brand loyal; treats hairpieces as recurring commodity
- **Goals:** Find cheapest price; test Superhairpieces without major risk
- **Challenges:** Inconsistent quality fears, high cost, slow shipping
- **Identifier:** Only purchases during promotions; does not buy regularly
- **What we provide:** Refer to sales page

---

### 8. The Supplies Buyer
- **Who:** Purchases supplies (tapes, glues, solvents) from us
- **Psychographics:** Focused on authenticity and performance of specific brands (Walker Tape, Professional Hair Labs); views us as a reliable distributor
- **Goals:** Consistently purchase authentic supplies at competitive price with zero downtime
- **Challenges:** Stock outs, pricing fluctuations
- **Identifier:** Only purchases supplies orders
- **Sales Process:**
  1. Confirm if they buy hairpieces from other providers
  2. If yes: ask where, what base material, how was the experience
  3. Provide supplies buying strategy; check procurement needs; note $200+ supplies order (under 3 lbs) = free shipping (retail only); remind Max Glue promotion; offer free supply item with hairpiece purchase
  4. If not satisfied, invite to free consultation
  5. If they provide a receipt from another vendor (within last 6 months), offer a one-time 10% discount stackable on existing promotions

---

### 9. The Salon Stylist
- **Who:** Hair stylist sourcing hairpieces from us
- **Psychographics:** Focused on profitability, client retention, and quality assurance; needs reliable supply to mark up confidently
- **Goals:** Source premium hair systems and professional supplies at wholesale/trade pricing
- **Challenges:** Inconsistent supply, needs competitive wholesale pricing, technical support
- **Identifier:** Created a professional account with business registration or barber license; Customer Group = Tier 1
- **What we provide:** Connect to Evolve Academy; free 1-hour trial class; send training materials; encourage store credits for first orders; provide product catalogs; refer price-sensitive clients to RHG

---

### 10. The Beauty Ambassador
- **Who:** Manages a social media channel about hairpieces
- **Psychographics:** Creative, motivated by content creation and personal brand credibility; seeks high-quality authentic products
- **Goals:** Create engaging content featuring Superhairpieces that their audience genuinely appreciates; wants fair compensation, free products, logistical support
- **Challenges:** Exclusivity terms, limited partner slots, limited budget, product quality concerns, complex logistics, authenticity alignment
- **Identifier:** Has an active social media account
- **What we provide:** Free products for sponsored videos; invite loyalty customers to partner; share Figma-designed assets and product specs; direct marketing support via Mateo (Gmail)

---

## Customer Journey

**Stages:**
1. Lead (signed up account only)
2. Consultation Scheduled
3. Consultation Done
4. Hairpieces Buyer
5. Base Cut
6. Service Customer
7. Frequent Buyer
8. Online Subscriber (auto-charged for new hairpiece on regular schedule)
9. In-Store Subscriber (auto-charged for hairpiece + store credits for installation/maintenance)

---

## Sales Teams

### Outreach – Jill
- Outreach sales rep visiting existing hair extension Tier 1 customers for product feedback
- Journey tracked in HubSpot: Attempted to Connect → Bad Timing → Not Interested → Phone Meeting Scheduled → Face to Face Meeting Scheduled → No Show → Met Once → Met Twice

### Bogota Call Centre (Follow-up Team)
- **Head of Office:** Andres
- **Sales Reps:** Cristian, Juan
- Follows up with customers who completed consultations but have not placed orders
- Process tracked in HubSpot Sales Hub: Attempted to Connect → Bad Timing → In Consideration → Closed Success → Closed Negative → Already Converted

---

## The 6 Core Questions (Hypothesis)

We believe that if a person aware of hair loss has these 6 questions answered, they will start getting hairpieces from us:

1. How does regaining my hair fundamentally change my daily confidence and social interactions?
2. Why is a non-surgical hair system often a more predictable and satisfying choice than the risks and recovery of hair transplant surgery?
3. Why do people choose Superhairpieces, a Canadian or North American brand, instead of choosing a foreign company?
4. What does a "day in the life" actually look like with a hair system — from morning showers to gym sessions and sleep?
5. How do I ensure my first hairpiece matches my natural hair color, density, and lifestyle without it looking "fake"?
6. What is the realistic annual investment for both the hair systems and the supplies needed to keep them looking fresh?

**Two additional questions:**

7. If I make a mistake during my first home installation or if the color isn't a perfect match, how will Superhairpieces help me fix it?
8. Why does the choice of adhesives and solvents matter as much as the hairpiece itself?
