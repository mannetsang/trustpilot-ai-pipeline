# AP credit-card reconciliation

Accounts payable (`ap@superhairpieces.com`) uploads a credit-card statement or
an RBC **Transaction Export** (one `.xlsx`/`.csv` with every card of the
account). The service in [`cloud_run/`](cloud_run/) stores each line once,
reads every invoice filed in the AP Invoice folder on Drive with Gemini,
pairs purchases with invoices, posts an *invoices needed* card to each credit
card's own Google Chat space, and serves a dashboard, organised by year and
month, where card owners record the use case (what each charge was for) and
the cost centre of each charge and link or waive invoices. AP manages cards,
use cases, cost centres, vendors and people on the admin pages at `/admin`.

Storage is a **dedicated Supabase project** (Postgres, schema in
[`schema.sql`](schema.sql)), separate from every other Supabase project the
company runs, reached from the server only with the service-role key. The
Cloud Run service is `ap-reconciliation` in `us-central1`, live since
2026-10-12 at <https://ap-reconciliation-onvg62bzra-uc.a.run.app> (the
`DASHBOARD_URL` the Chat cards link to; the same service also answers at
`https://ap-reconciliation-304363458561.us-central1.run.app`). Its public
sign-in page, the address to share, is the second service
`ap-reconciliation-login` at
<https://ap-reconciliation-login-onvg62bzra-uc.a.run.app> (see "Access and
roles").

## The Invoice folder

Invoices and receipts live in `ap@superhairpieces.com`'s My Drive, folder
**Invoice** (id `18YkGTNDIrxdQgHRgquvQizI8fLsFOpPk`, env `INVOICE_FOLDER_ID`),
laid out as

```
Invoice/
  2026 09/
    Canada Post/      invoice-2026-09-04.pdf
    Amazon/           order-112-...pdf  receipt.jpg
    Google Cloud/     ...
    stray-receipt.pdf           (allowed, no vendor hint)
  2026 10/
    ...
```

- The month folder is `YYYY MM` (a space, zero-padded month; `2026-09` and
  `2026_9` are accepted too). Anything not shaped like a month is skipped
  unless it sits below a month folder.
- The first folder level below the month is the **vendor folder**. Its name
  travels with every file as a matching hint, so a receipt under
  `2026 09/Amazon` is treated as an Amazon charge from around
  September even when the document itself is hard to read.
- PDF, PNG, JPEG, WebP, HEIC and plain-text files are read; Google
  Docs, Sheets and Slides are exported as PDF first; shortcuts are followed.
  Word, Excel, zip, GIF and e-mail files are **not** read: save them as PDF.
  PDFs over 15 MB and images over 7 MB are skipped.
- The service reads the folder as the AP user, with the user OAuth refresh
  token in Secret Manager `google-workspace-ap-*` (the Business Profile
  scripts use the same mechanism). When those three values are missing it
  falls back to Application Default Credentials, which only works if the
  folder is shared with the runtime service account.

A file is read **once**: the result is stored by Drive file id and the file
is only re-read when its `modifiedTime` changes. Moving or renaming a file
updates its folder hints without another model call.

**Receipts added from the dashboard** (*Add receipt...*, on the cards in
`RECEIPT_UPLOAD_CARDS`, today card 4421) land in the same layout:

- The month folder is the purchase's transaction month. An existing folder
  is reused however it is spelt (`2026 09`, `2026-09`); only when there is
  none is `YYYY MM` created.
- The vendor folder is suggested from the charge: an existing folder of
  that month whose every word appears in the description (`Uber* Trip` goes
  to `Uber`, never `Uber Eats`), else a folder of the last twelve months,
  else a cleaned name (`Sq *coffee Shop Toronto` becomes `Coffee Shop`,
  `Air Can*` becomes `Air Canada`). The person sees *Saves to Invoice ›
  2026 09 › Uber* and can change it; a typed name snaps to an existing
  folder of the same letters (`uber` stays `Uber`), and a name that is a
  path or a month is refused in favour of the suggestion.
- The file is `<date> <vendor> receipt.jpg` (or `.pdf`), with `(2)`, `(3)`
  when taken. Never the amount or card digits: the file name is a hint
  the reader sees. Several photos of one receipt are saved as **one PDF**
  (each photo kept as is), so the pages of one receipt never become
  separate invoices. The browser shrinks photos to at most 4.5 megapixels
  (JPEG, which also drops the photo's location data) before sending.
- Who and why ride in the file's Drive description and app properties
  (purchase id, uploader), which the reader never sees.
- The purchase is linked at once (`matched`, by hand, *receipt uploaded
  by ...*), then the file is read in the background; when its total, card
  digits or date disagree with the charge the row shows *check receipt*
  with the reason, and the link stays until a person decides.

## Pipeline

1. **Upload** (`POST /api/statements`). `.xlsx`, `.xlsm`, `.xls`, `.csv` and `.tsv`
   exports with a recognisable header row (`Transaction date`, `Supplier`,
   `Amount`, `Account ****-****-****-1610`, `Cardholder first/last name`,
   `Source currency/amount`, `Billing currency`...) are parsed directly, no
   AI involved. So are `.txt` exports, and a text export saved into a Word
   document (`.docx`, read paragraph by paragraph, or a Word table row by
   row), including **headerless** ones such as the New Frontier Global card
   export (`00000003,"4514xxxxxxxx3914",20260513,0,"PAYPAL *...","USD",000002025.00`):
   the columns are recognised from their contents (masked card number,
   `YYYYMMDD` date, `0`/`1` debit-credit flag where `1` is a payment or
   credit, description, currency code, amount), and the upload warns that
   it did so. PDF statements, photos and spreadsheets whose columns are not
   recognised go to Gemini with a fixed output schema. Every line gets a
   deterministic id (card, dates, normalised description, amount, currency,
   plus a counter for identical lines in one file), so uploading the same
   export twice, or an overlapping one, adds nothing. Cards seen for the
   first time are created in the `cards` table; credits, payments and fees
   are stored with status `not_required`. With `STATEMENTS_FOLDER_ID` set a
   copy of the upload is kept on Drive under the month's name.
2. **Index** (`POST /api/index`). Walks `Invoice/<month-1>`, `<month>` and
   `<month+1>`, downloads each new or changed file and asks Gemini for the
   vendor, invoice number, date, total actually charged, currency, printed
   card digits and a one-line summary. `INDEX_BATCH` (30) files per call on
   `INDEX_WORKERS` (4) threads; the answer's `remaining` count tells the
   dashboard to call again until it is 0.
3. **Match** (`POST /api/match`). Card charges an invoice lists first, then
   rules, then the Gemini judge for the rest; see the next section.
   **Orders charged in parts.** An Amazon order shipped in several boxes is
   charged per shipment (item prices before tax, plus tax and any fees), and
   the order PDF lists every charge under "Credit Card transactions" (card
   digits, date, amount). The reading keeps that list
   (`invoices.card_charges`), and `matching.charge_match` pairs each charge
   with the purchase of the same amount to the cent, on the same card (the
   charge's own digits), dated the day before, the same day or the day after
   (the purchase or the posting date): matched, no judge involved. Such an
   invoice is shared, one transaction per charge (`transactions.invoice_part`,
   the charge's index), and is never linked whole; any other invoice links to
   one transaction. The database refuses a second link of the same charge, a
   second whole link, and a whole link beside charge links (indexes
   `transactions_invoice_whole_unique` / `_part_unique` and the trigger
   `transactions_invoice_link_guard`). *Link…* on such an order takes its free
   charge on the purchase's card with the same amount (else the closest), and
   says which; the picker shows "1 of 2 charges linked". Invoices read before
   the list existed are read again once with `python main.py reread --vendor
   amazon` (run as the sync job with `--args=main.py,reread`; it carries on
   where it stopped when run again): links made whole to an order that turns
   out split move to the charge they are, and the months those orders serve
   are queued for a full match.
   **Bills charged later (pre-authorized payments).** A bill may say when its
   total is charged to the card: TELUS prints "The total due will be charged
   to your credit card on October 09" (account 606763692) or "...15 days
   from your bill" (TELUS Mobility). The reading keeps that date
   (`invoices.charge_date`; worked out when it is given in days; dropped
   unless it falls 7 days before to 62 days after the bill), and the rules
   and the judge compare a purchase with it instead of the bill date. A
   purchase more than 10 days from it (`CHARGE_DATE_DAYS`) is never paired
   with that bill: TELUS charges $50.85 every month, and the October 13
   charge pays the September 16 bill, not the October 16 one dated three
   days later. Bills read before the date existed are read again with
   `python main.py reread-charge-dates --vendor telus` (as the sync job:
   `--args=main.py,reread-charge-dates,--vendor,telus`), which also lets go
   of the links that fall outside their bill's date and pairs those
   purchases again (links made by hand stay).
   **Invoices paid in charges they do not list.** Square bills its
   subscriptions on one tax invoice (CAD 818.12) and charges each separately
   (535.62, 237.30 and 45.20 the same day). After the charge and rule passes,
   `matching.infer_split_charges` looks at each invoice still unlinked that
   lists no charge (or only its whole total). If exactly one set of 2 to 6
   purchases adds up to its total to the cent, the set is recorded as its
   `card_charges` (each marked `inferred`) and linked like an Amazon order's
   charges. The purchases must be on one card, from a vendor resembling it,
   within a day of its date, and still need an invoice. The month before and
   the month after are searched too. Nothing is recorded in these cases:
   - one purchase pays the invoice whole;
   - a purchase is the whole of another unlinked invoice;
   - the total can be made two ways;
   - a purchase would serve two invoices.
   A re-read that agrees on the total, currency and date keeps the inferred
   charges. Any other re-read lets the links go so the matcher looks again.
   **Invoices charged late.** A billing centre may charge a weekly invoice
   weeks after it: in 2026 UPS 449087 and Purolator 3190186 took up to 61
   days. An invoice dated 22 to 70 days before a purchase (`LATE_PAYMENT_DAYS`)
   is its payment (`matched`, 0.9, "paid N days after the invoice date") when
   all of these hold:
   - same amount to the cent, in the same currency;
   - a vendor the descriptor resembles, and no other card shown;
   - no other invoice of the vendor with that amount in the window, whether
     or not it is linked (a monthly bill of one amount is no late payment);
   - no other purchase of that amount on the card in the 70 days after the
     invoice;
   - the card's statements reach the end of those 70 days.

   Invoices filed up to three months back are loaded for this alone
   (`only_late`): no "possible" match, no split and no judge draws on them.
   **One charge paying several invoices.** UPS charges a week's customs-
   brokerage invoices as one payment (117.31 + 20.23 = 137.54); a billing
   centre catching up pays several weeks at once (Purolator 7,149.19 for three
   invoices). A purchase still without a match is linked when 2 to 4 unlinked
   invoices meet all of these:
   - one vendor folder, in its currency;
   - dated 0 to 70 days before it and within 35 days of each other;
   - their totals add up to it to the cent;
   - no single invoice of that vendor has the amount;
   - this is the only such set, or the only set whose invoices are all dated
     the same day (UPS bills a 20.23 fee most weeks);
   - a set dated over several days has no amount the vendor bills again
     around then (another week's copy of a fee proves nothing);
   - no other purchase of the amount could be its payment: in the run, or
     in any month on the same card from the vendor.

   The largest invoice is `invoice_id`. The others go in
   `transactions.also_invoice_ids`, the documents paid or confirmed with it,
   and the dashboard lists them under the invoice.
   **Statements that list their payments.** A Google Ads monthly statement
   ("Payments received") and an insurer's premium payment history (Cigna)
   are no invoices (`is_invoice` false). The reading lists the card payments
   they record as `card_charges`, and such a statement is shared one payment
   at a time like an Amazon order. Statements listing fewer than two payments
   stay out of matching.
   **Payment confirmations.** A UPS "Payment Confirmation" e-mail or a billing
   centre's receipt says a payment was made; the invoice it pays says what was
   bought. The reading types it `payment_confirmation`, with the invoice
   numbers it names (`invoices.paid_invoice_numbers`). A purchase the rules
   find on a confirmation is linked to the invoice instead when that is in
   Drive:
   - the invoices it names, when they add up to the purchase;
   - otherwise the one unlinked invoice of its vendor folder with the exact
     amount, dated 0 to 21 days before it, or paid late as above.

   When the confirmation names no invoice and no single one fits, the
   invoices it pays together are looked for as above. The confirmation goes
   in `also_invoice_ids`. A purchase linked to an invoice also gets the
   unlinked confirmations from the same vendor folder that name that invoice.
   A confirmation naming none is attached when it meets all of these:
   - it has the exact amount (compared in cents: UPS's e-mails print only
     "$" and read as USD) and is dated within five days of the purchase;
   - it is the only such confirmation;
   - the purchase is the only one of that amount on its card in those days.

   The month run, the Invoice watch and *Rematch* do this, also for purchases
   the rules or the judge matched earlier, but never for links made by hand.
   Adding a confirmation to a link never moves the invoice: if it cannot be
   added (someone linked it elsewhere meanwhile), the link stays as it was.
   *Rematch* never trades a sound link to an invoice for a confirmation
   alone. The month run also lets an exact invoice found since replace a
   judge's match whose amount was not exact: a late payment, or invoices paid
   together. Judge matches with no exact replacement stay as they are.
   Documents read before these types existed are read again with
   `python main.py reread-documents --vendor ups` (as the sync job:
   `--args=main.py,reread-documents,--vendor,ups`). It writes only the type,
   the invoice numbers and a statement's payments, and queues the months they
   serve. A document in `also_invoice_ids` counts as linked. The database
   refuses it as another row's invoice or `also` document (trigger
   `transactions_invoice_link_guard`), and a row has none without an invoice.
   When one leaves Drive or is read again (`matching.release_stale_also`):
   - a confirmation that is gone is dropped;
   - if an invoice paid with the charge is gone, or the invoices no longer add
     up, the purchase is matched again;
   - a reading that failed changes nothing.

   Links made by hand stay; their checks show the difference.
   **Reissued cards.** A merchant may show a card's new number while the
   statement still shows the old one (Amazon 5026 for 5018, Square 4813 for
   4805, Freightcom 7444 for 7436). The account number moves by one or two
   and the check digit is recomputed (Luhn). A number no statement card has
   that is the reissue of exactly one statement card counts as that card;
   *Cards…* lists them under "receipts show".
4. **Notify** (`POST /api/notify`). One Chat card per credit card that still
   has purchases without an invoice, listing the missing purchases, the
   possible matches to confirm and the number of charges without a cost
   centre, with *Open dashboard* (deep link to the card's month) and
   *Invoice folder* buttons. Every post is logged in `notifications`.
5. **Weekly reminder** (the Cloud Run job `ap-reconciliation-remind`, which
   Cloud Scheduler starts on Mondays 14:00 UTC; it runs `python main.py remind`).
   For each card, the newest month that still has missing or possible
   purchases is re-indexed, re-matched and notified again.
6. **Drive import** (the Cloud Run job `ap-reconciliation-sync`, which Cloud
   Scheduler starts every 15 minutes; it runs `python main.py sync`).
   Statements put in the **Bank Transactions** folder in Drive
   (`BANK_FOLDER_ID`, nikki@'s folder, shared with ap@ as a viewer), or in
   any folder inside it, are uploaded by themselves: a Google Doc is read as
   its plain text (a bank's text export pasted in), a Google Sheet as
   `.xlsx`, and `.xlsx/.xls/.csv/.txt/.docx/.pdf` and photos as they are. Each
   file is read once per version (its modified time, kept in
   `statement_sources`); a version changed in the last 10 minutes waits, so a
   sheet someone is still filling in is not read half-way. A changed file
   adds only its new lines (line ids), and says so when lines of the version
   read before are gone from it. Then the months those lines fell in are read
   for invoices and matched, newest first. Nothing is posted to Chat:
   reminders stay with *Send reminders* and the weekly job. A file that
   cannot be read is listed with the reason and read again when it changes,
   or when a newer reader no longer refuses it (`OLDER_READER_REFUSALS`: the
   `.xls` exports turned down before `.xls` could be read are read again by
   themselves); a Drive or Gemini failure is tried again on the next two runs. A file
   read for the first time has all its months matched (its lines may have
   been uploaded by hand before), a new version only the months of its new
   lines. Months not matched yet are kept in `app_state`: a run starts no
   month after 25 minutes, lets the job lease go for ten seconds between
   months (a waiting dashboard step or the weekly reminder takes it, and the
   run stops there), and the next run matches what is left, together with
   the months of anything it imports. They are matched even while the folder
   cannot be read. Removing an imported upload in the dashboard (*Remove*
   under Uploads) also stops its file from being read again; copy the file
   in Drive to have it read as a new one.
   A line already stored under another id is not added again: same card,
   same date, the same amount whatever its sign, and a description that says
   the same. That covers statements uploaded by hand before (Gemini's
   reading, or this parser's before a fix changed a currency or a sign, both
   part of a line's id); the upload's warnings say how many such lines it
   met and how many read differently.
7. **Invoice watch** (the same job, every run). An invoice filed under
   `Invoice/YYYY MM/<vendor>/` since the last run is read and matched by
   itself: invoices the e-mail filer (`mannetsang/ap-automation`,
   `invoice-filer/`) saves from ap@'s Gmail, invoices filed by hand,
   receipts uploaded from the dashboard. The filer reads Gmail, not
   `EmailattatchmentOriginal/`; if e-mailed invoices stop arriving, check its
   GitHub Actions runs first.
   Drive's change feed for ap@ says what moved (`app_state`
   `invoice_watch` keeps its page token, the month folders still to read,
   and `to_match`: every file the feed saw filed in a month folder, and
   everything read, until it has been matched). Each month folder with a
   new, changed, moved or removed file is read like step 2, then
   `to_match` is matched by `matching.match_new_invoices`: rules first,
   then the Gemini judge, only between those invoices and the purchases of
   the months that may draw on their folder (its own and the months either
   side) still missing an invoice or holding a possible match, and the
   judge sees only the purchases a new invoice could document (amount within
   15%, or not comparable and the vendor alike; no other card; dates under
   45 days apart). Nothing is taken away: a possible match is replaced only
   by a better one (or when its invoice is gone from Drive), a match or a
   link made by hand is never touched, and a pair that would not be kept
   never takes an invoice another purchase needs. An invoice moved to the
   right month, restored from the trash, or read first by someone's Rematch
   is matched too. One not read yet, whose read failed, or that a failed
   judge call could not place is tried again on the next runs (four in all;
   at most `INVOICE_JUDGE_PER_RUN`, 15, new invoices go to the judge per run,
   the most recently read first; the rest get the rules and wait their turn
   without spending a try; a file the feed lists that was read before the
   last run, as when a folder is shared and every file in it changes, is not
   matched again;
   a failed read queues its folder again). Invoices already read that a
   moved or renamed vendor or month folder puts in a new month are matched
   there too. A watch step that raises is noted on the import line and the
   run goes on (the statement months still run); a month folder that fails
   four runs in a row is let go until something in it changes, and a reading
   the database refuses is stored as "could not be stored" without losing
   the rest of its batch. The watch runs only under the
   job lease: a run that finds the lease taken does not read the feed, and
   the next one reads the same changes; a Drive rate limit or server error
   keeps the run's place in the feed. The first run starts the feed and
   reads this month's folder and the one before. Files under `Invoice/` but
   in no month folder are not read. The dashboard's import line says what
   the last run read and matched, and the rows reload. `INVOICE_WATCH=0`
   turns it off.

`POST /api/statements?sync=1` runs steps 1-4 in one request (the CLI, curl
and the tests use it); the dashboard's *Upload and process* button runs them
as separate calls so it can show progress. Jobs serialize on an in-process
lock: a second index, match, notify or synchronous upload gets `409 busy`
while one runs. The two Cloud Run jobs run in their own containers, so the
store holds a lease as well (`job_lease`): every long step, the dashboard's
and the jobs', takes it and renews it every minute, and one that died loses
it after five minutes. The dashboard answers `409` with
`"elsewhere": {"job": "drive-sync"}` while the import runs (the page says it
is waiting for the import); the import skips a run while the dashboard holds
it; the weekly reminder waits up to 25 minutes. The dashboard takes over a
lease an earlier instance of itself left (a deploy mid-step), since it runs
on one instance. While a job holds it, the page waits on
`GET /api/jobs/current` (which then carries `elsewhere`) for up to 45
minutes before sending its request again, rather than resending it every
few seconds.

## Matching rules and statuses

Every purchase is `matched`, `possible`, `missing` or `waived`; credits,
payments and fees are `not_required`. Matching only ever touches purchases
that are `missing` or `possible`, and those the rules or the judge matched to
a payment confirmation (moved to the invoice it pays) or to an invoice (given
its confirmations): a manual link, confirmation or waiver is never undone by
a re-run.

**Rule pass.** Each pending purchase is scored against every unused invoice
of the surrounding month folders:

| Signal | Score |
|---|---|
| Amount: invoice total against the billed amount, or against the original foreign amount when the invoice is in that currency | equal (within one cent) 1.0, within 1% 0.6, within 5% 0.3 |
| Vendor: token overlap between the statement description/supplier and the invoice's vendor, vendor folder and file name (`Amzn` = Amazon, `ChatGPT` = OpenAI, `Goog` = Google and a few more aliases) | 0 to 1 |
| Date: invoice date vs transaction date | 3 days 1.0, 7 days 0.8, 14 days 0.5, 35 days 0.2 |
| Card digits printed on the invoice | a different card rules the pair out |

An exact amount with a vendor overlap and an invoice date within three
weeks (or no date on the invoice) is `matched`; an exact amount with only
one of the two (the vendor, or a date within three weeks) is `possible`; an
amount within 1% with a strong vendor overlap and a date within 10 days is
`possible`. The same amount on its own, from another vendor and weeks away,
proposes nothing: with three months of invoices in the pool it is usually a
coincidence. Pairs are taken best-first, an invoice is linked to at most one
transaction, and a pair someone rejected with *Not this* / *Unlink* is never
proposed again. Credit notes and files no longer present in Drive stay out
of the pool.

**Gemini judge.** What is still unmatched (purchases and invoices) goes to
the model in batches with the same instructions a person would get: same
business, same amount (allowing tips, exchange rates or separate tax when
it says so), invoice date within about two weeks, one invoice per charge,
never a match on amount alone. Confidence 0.85 and up is `matched`, 0.6 and
up is `possible`, the rest stay `missing`. The model's answer is still
checked against the numbers: a proposal whose invoice names another card is
dropped, one whose total is more than 15% off any comparable amount is
dropped, and one that is off by more than 2% can only be `possible`. The
`match_note` on each transaction says which pass decided and why.

**Manual actions** (the `action` field of `PATCH /api/transactions/<id>`):
`confirm` a possible match, `link` an invoice chosen from the picker,
`unlink`, `waive` (no invoice exists, with a reason) and `unwaive`.
`unlink` (*Not this*) refuses the invoice and every document that came with
it (`also_invoice_ids`); `link` replaces them all with the one invoice chosen.
`confirm` applies only to the proposal the dashboard showed: if a match run
released or moved it meanwhile, the answer is `409` *This purchase changed
meanwhile; reload.* A receipt added with *Add receipt...* is a manual link
too: matching never moves it; *Remove...* (which also sends the file to the
Drive trash) or *Unlink* undoes it. *Remove...* marks the file's invoice
row first (its `extraction_error` starts *upload withdrawn*), so neither the
index nor the matcher takes the file again, even when the Drive trash fails
or the file is later restored from the trash (add it again with *Add
receipt...* if it was wanted after all). The month-wide matcher writes each
row only if nobody changed it while it ran (compare and set), so an upload
or a *Link* made during a run is kept; the result counts such rows as
`skipped`.

## Access and roles

**The live dashboard signs people in with Google**, decided 2026-10-13:
**Identity-Aware Proxy** (IAP) on the Cloud Run service. The service has no
public invoker (`--no-allow-unauthenticated --iap`), Google signs the person
in before any request reaches the container, only accounts on IAP's access
list (the `superhairpieces.com` domain) get through, and the app verifies
the signed header `X-Goog-IAP-JWT-Assertion` IAP adds (ES256, IAP's
published keys, `IAP_AUDIENCE` as the audience, issuer
`https://cloud.google.com/iap`), takes the e-mail from it, requires the
domain to be in `ALLOWED_DOMAINS` and derives the role:

| Role | Who | May |
|---|---|---|
| `ap` | the addresses in `AP_EMAILS` (`ap@`, `manne@`, `snow@`, added 2026-10-20); everyone, in password and open mode | upload, index, match, notify, edit cards, edit every transaction; manage cards, use cases and cost centres, vendors and people in `/admin` |
| `member` | anyone else in the domain (IAP mode only) | read everything; edit only the transactions of cards whose **owner e-mail** (set by AP under *Cards...*; one address or several, separated by commas) is theirs, which includes *Add receipt...* on cards with receipt upload. `/api/transactions` returns an `editable` flag per row and the page disables the rest. Card 4421's owner is `anais@superhairpieces.com` (2026-10-15); add Bo Li's address next to it to let Bo Li add receipts too |

In IAP mode nothing on the dashboard service is reachable without IAP: it
has no `allUsers` invoker, only IAP's service agent, and the app answers 401
to any `/api` call without a valid assertion (the sign-in page below is a
separate service, public on purpose, and holds nothing: no data, no
secrets). Because identity rides on a browser cookie
there, as it does in password mode, changing calls (POST, PATCH, DELETE) in
both modes must also carry `X-Requested-With: XMLHttpRequest`, which the page
sends on every call and a form on another site cannot; `curl` adds
`-H "X-Requested-With: XMLHttpRequest"` (and, in password mode, the cookie
from a `POST /api/login` with `-c`/`-b`; the login call needs the header too).
IAP's public keys are cached in the process for an hour. There is no OAuth
client and no token in either page in any mode: the dashboard has no
sign-in button of its own, and the sign-in page's Google buttons are plain
links to the dashboard.

**The admin pages** (`/admin`, see "Admin pages" below) sit behind IAP like
`/`: the page itself holds no data, and every `/api/admin` call checks the
`ap` role, so a member who opens the address sees "These pages are for
accounts payable only" and gets 403 from the API. Its writes need
`X-Requested-With` like every other change, and are refused (403) in open
mode against the real database, where anyone who reaches the URL would be
AP; reads stay allowed there.

**The sign-in page.** IAP guards every path of the service, so a page that
shows before sign-in cannot live on it: `login/` is a second, public Cloud
Run service, `ap-reconciliation-login`, one static page served by nginx
with the logo, a *Continue with Google* button (the dashboard URL: IAP signs
the person in, silently when the browser is already signed in to Google)
and *Use a different account* (the dashboard's
`/_gcp_iap/clear_login_cookie`, which clears IAP's cookie and makes Google
ask which account to use). The buttons follow Google's *Sign in with
Google* branding (white, grey border, the four-colour G, Google Sans Medium), so
people recognise them; they are links, not Google's sign-in library. The
dashboard's *Sign out* clears IAP's cookie the same way and lands on this
page with `?signed_out=1`, which says the dashboard session is over and
that the Google account itself stays signed in to the browser, offers *Sign
in with Google* through the account picker, and links Google's own
sign-out for shared computers. That is the whole of what signing out can mean behind IAP: IAP
has no signed-out state beyond its cookie, and a visit straight to the
dashboard URL from a browser still signed in to Google is signed in again
without a prompt. The page learns the dashboard URL at container start
(`DASHBOARD_URL`, see `login/40-dashboard-url.sh`) and the dashboard learns
the page's URL as `LOGIN_URL`; the deploy workflow sets both.

Two other modes exist, one edit of the deploy workflow away (step 5):

- **Open** (`--allow-unauthenticated --no-iap`, `AUTH_DISABLED=1`; the live
  setting from 2026-10-12 to 2026-10-13): anyone who can reach the URL is
  `ap`; the name typed into the *Your name* box in the header (remembered by
  that browser, sent as the `X-Dashboard-User` header) is what *Updated by*,
  *uploaded by* and the notification log record, `anonymous` when the box is
  empty. The URL is then the only thing between the internet and the card
  data.
- **Shared password** (open mode plus `DASHBOARD_PASSWORD`, Secret Manager
  `ap-reconciliation-dashboard-password`): the page asks for the password
  first. `POST /api/login` checks it and answers with a signed cookie
  (`ap_recon_session`: HttpOnly, SameSite=Lax, Secure on Cloud Run, 30 days
  or `DASHBOARD_SESSION_DAYS`). The cookie is `<issued>.<hmac>`, signed with
  a key derived from the password and the Supabase service key: nothing is
  stored server side, a stolen cookie reveals neither secret, and a new
  version of either signs everyone out. Wrong passwords are counted per
  address (10 in 15 minutes) and overall (50), after which `/api/login`
  answers 429 for the rest of the window; the counters live in the one
  instance. *Lock* in the header forgets the cookie on that browser. A short
  password is a lock on the door, not a vault.

## Dashboard

- **Google sign-in**: Identity-Aware Proxy signs people in before the
  page loads (the dashboard has no button of its own; anyone already signed
  in to Google goes straight in, which is also what the Chat card links
  do). The public sign-in page (above) is the address to share and where
  *Sign out* lands. *Switch account* in the header clears IAP's cookie for
  this site and shows Google's account picker; *Sign out* clears it and
  lands on the sign-in page, with the Google account still signed in to
  the browser (the button only shows once the dashboard knows the page's
  address, `LOGIN_URL`). Back after *Sign out* reloads the dashboard rather
  than showing the previous person's cards from the browser's cache. An ended
  Google session shows a *Reload* gate. (In open and password
  mode a *Your name* box in the header, remembered by the browser, is what
  *Updated by* records; password mode asks for the password once per
  browser for 30 days, and *Lock* forgets it.)
- **Months** down the left (newest first), each with its spend and badges for
  missing and possible invoices; the URL hash is `#/YYYY/MM?card=1610`, which
  is what the Chat card buttons open.
- **Tiles** for the month (or the selected card): spend, purchases, invoices
  found, missing, possible, waived, and **No use case**: purchases whose Use
  case box is empty. That is what "uncoded" means everywhere (the tile, the
  filter, the month list, the Chat card and the voice assistant): a cost
  centre alone does not code a purchase.
- **AP panel** (AP only), one row: a slim drop zone for the statement
  (drop the file on it or click it), *Upload and process*, then
  *Re-run matching*, *Send reminders*,
  *Cards...* (label, holder, group, card number, linked under, name on
  card, owner e-mail, Chat space name, active flag, and whether a webhook is
  configured for the card; a save there leaves the card's default cost
  centre and receipt-upload switch, which live in `/admin`, as they are),
  *Admin* (the admin pages), plus the step list
  of the running job. Nothing is posted to Chat on its own: after matching
  (and on *Send reminders*) the page lists which cards would get a reminder,
  with their counts and Chat spaces, and waits for *Send* or *Not now*.
  *Re-run matching* reads new invoices for the month on screen and matches
  all its cards again. While something runs, the button in another month
  reads *Queue July* and lines that month up; the step list shows *now
  September 2026, next July 2026 ×, ...* (× takes a month out). The ▾ next
  to it offers *Re-run all months* (newest first) and *Clear the queue*.
  Queued months run one after another, and the reminder question comes once
  at the end for every month that finished. A month an upload is still
  matching reads *Matching…* and cannot be queued twice. Problems from the
  runs before stay above the steps under *Earlier in this run*, and the
  Details lines are kept. The queue lives in the open page: keep it open
  until it is done (leaving asks first).
  Under the row, one line on the **Drive import**: when the Bank
  Transactions folder was last checked, the last statement imported from it,
  files still being edited, months still to match, and how many files could
  not be read; *Files read* lists each file (read, not read with the reason,
  trying again) with its lines and months, linked to the file in Drive. A
  folder ap@ cannot open turns the line red and says to share it. The page
  asks every minute while it is open, and when the import has read something
  new it says so and reloads the months (not while a step of its own runs).
- **Card chips** ("1610 - Yin Wei", the full label on hover; one line that
  scrolls sideways when there are more cards than fit) and a status filter
  (all, missing, possible, matched, waived, no use case), a search box
  over description, amounts, cost centre, use case and person, and on phones also a
  card drop-down (the chips, with their counts, still show above it).
- **Transaction table** (click a header to sort, drag its right edge to
  resize, *Columns* to hide columns or reset; the browser remembers all
  three). It fits the window: on a narrow screen the columns give way in
  proportion to what they can spare (a column sized by hand keeps its
  width while the others can still shrink), down to a minimum that keeps
  the invoice buttons on one line; only below that does the table scroll
  sideways. While you drag an edge the other columns hold still, so the
  edge follows the mouse; the table is fitted again when you let go. While it fits, the header row stays pinned under the top bar
  as the page scrolls. The Card column is left out while one card is
  picked. Columns: date, card, description with city/country, amount
  (with the original foreign amount), cost centre dropdown (the active
  places of the `cost_centers` table, with a *Custom…* free text), use case,
  note, the **person** the transaction is assigned to, and the
  invoice column: the linked file with vendor, number and total, or the
  buttons *Confirm* / *Not this* on a possible match, *Link...* / *No
  invoice* on a missing one, *Undo* on a waived one. Edits save on change.
- **Use case** (what the purchase was for; stored in `transactions.usage`).
  A pick-list of the use cases AP keeps in `/admin` (those for any group
  plus those of the card's group, in AP's order, *Other* last), then *Type
  something else…*, which turns the cell into a text box that saves free
  text when you leave it (empty or Esc gives up). A mouse pick saves at
  once; with the keyboard, arrowing through the closed list saves nothing
  until Enter or leaving the box, so passing through the list is not a
  save. A typed name that matches a listed one, case and spaces aside, is
  stored with the list's spelling. When the purchase has no cost centre
  and its card has a **default cost centre** (set in `/admin`), saving a
  use case fills the cost centre too, and the toast says so (*Use case saved
  · cost centre set to Salon - Dufferin*); until then the Cost centre box
  shows that default greyed (*— Salon - Dufferin when the use case is set
  —*). A cost centre already there is never replaced, not even one saved
  from the Cost centre box while the use case was being saved (the use case
  is still saved), and a supplementary card does not borrow its primary's
  default. A use case typed into the
  Cost centre box (say *Marketing*) is stored as typed, and a toast points
  to the Use case box, because the purchase still has no use case. When AP
  has switched **coding suggestions** on (`/admin`, Settings) and the
  vendor registry knows the purchase's vendor, an uncoded purchase shows a
  *Use Shipping & Logistics* button under the box (its tooltip says which
  vendor and pattern suggested it); nothing is filled in until someone
  presses it or picks a value.
- **Cards** (*Cards...*, AP). Each card is the account number the bank's
  export shows (its last four digits). Its row holds:
  - **Holder**: who uses the card. Saving a new holder adds them as a person
    and keeps a label still in the *Card ending 4431 - George* form in step.
  - **Group** (`cards.group_name`: Hair, Beauty): the team the spending
    belongs to.
  - **Card no.** (`card_number`): the last four digits printed on the card
    when they differ from the account's (account 4431, card 4449). Receipts
    showing it are this card's. One number belongs to one card: a number
    another card has, or a reissued number receipts already show for another
    card, is refused.
  - A group keeps the spelling its cards have (`hair` joins *Hair*).
  - **Linked under** (`primary_last4`): a supplementary card's primary card,
    one level (7436 under 4431). The card strip, the table and the export say
    "under 4431 (George)"; the primary lists its supplementary cards.
  - **Name on card**: the name printed on the card or the statement when
    someone else uses it (9456 is printed Henry Li and used by Linda).

  The **group picker** next to the card picker (shown once a card has a
  group; remembered per browser) narrows everything below it to that
  group's cards: the card strip, the table, the counts and the spend, which
  stays one total per currency. *No group* shows the cards in none. **Export
  missing** downloads an Excel file of the purchases still missing their
  invoice, for the group or card on screen, this month or all months
  (`GET /api/export/missing`). It has one row per purchase, with the group,
  account, card number, primary card, holder and person, cost centre and
  use case (the column was headed *Usage* before November 2026), plus a
  *Totals* sheet per group and currency.
- **Person** (the `people` table; `transactions.person_id`). Each transaction
  can be assigned to a person - who made the purchase, or answers for it -
  from a drop-down of everyone added so far. A purchase nobody assigned
  shows the card's holder, greyed and in italics; search, sort and the export
  count it as theirs. The empty choice shows that holder (or *— none —* when
  the card has none) and clears an assignment. *New
  person…* turns the cell into a name box: Enter (or leaving it) adds the
  person and assigns the row, Esc gives up. A name already there, case and
  extra spaces aside, assigns that person instead of adding a second. Anyone
  who can open the dashboard may add a person; assigning follows the other
  edits (AP on any card, a card's owners on theirs). A line assigned to
  someone is kept when its upload is removed, like one with a cost centre.
- **Voice mode** (*Voice* in the top bar, remembered per browser). The page
  says what is happening: a step of an upload, *Re-run matching* or *Send
  reminders* starting, finishing or failing, a *Rematch* result, an error
  (progress ticks stay quiet). A line being spoken is never cut by later
  news: outcomes wait their turn (at most three), and a "running" line is
  said only when nothing more important waits; an answer or a failure
  speaks at once. Nothing is said into the open microphone (the news
  follows once it closes), and after a reload the browser stays quiet until
  the first click or key press on the page, which then says the latest
  news. On a narrow screen the question box has its own row. And it answers questions about the month on
  screen (and the card tab picked): click the microphone and ask ("how many
  invoices are still missing?", "what does Bo Li still need to send?", "is
  anything running?"), or type the question. `POST /api/ask` condenses the
  month into a brief (counts per card, the purchases still missing or
  possible, the running or last job, uploads, reminders sent) and Gemini
  (`GEMINI_EXTRACT_MODEL`) answers in two or three sentences, which the
  browser reads aloud with its own voice (Windows' natural voices when
  there). Gemini gets one try of up to 20 seconds and at most two questions
  wait on it at once (one per browser; another is turned away until the
  answer arrives); without Gemini, if it fails or is busy, a plain summary
  of the month is the answer. A background job is named with the month and
  card it works on, so another month's numbers are never read out as this
  month's. Read only: a question cannot change anything, and text from
  bank files and invoices goes to the model as data. Listening uses the
  browser's speech recognition: Chrome and Edge (which send the audio to
  their speech service); other browsers type the question. Anyone signed
  in may ask; everyone can read every card anyway.
- **Rematch** on each purchase that is missing an invoice, has a possible
  match, or was matched automatically (not on waived rows or on invoices
  someone linked or confirmed by hand; *Unlink* those first). It is for the
  usual case of an invoice filed in Drive after the reminder: the server
  first reads any invoices newly filed in the purchase's month folder and
  the months either side, then runs the same rules and Gemini judge for
  that one purchase, against the invoices no other purchase holds;
  invoices refused with *Not this* are never proposed again. When the
  rules find only a weak possible match, Gemini is asked too and the
  better of the two wins. A rematch never loses a good link: the current
  invoice stays unless something better turns up (a match beats a
  possible match, then the higher confidence), so a row Gemini matched
  keeps its invoice when Gemini is down; only an invoice that is no
  longer a candidate (removed from Drive, moved away) is let go when
  nothing replaces it. The row is written only if nobody changed its
  invoice meanwhile (compare and set): a *Confirm*, *Link*, *Not this*
  or *No invoice* made while it runs always wins, and the message says
  so. The message also names new files in Drive that could not be read,
  instead of asking for the invoice to be filed again. It runs as a
  background job the row polls every second: the row shows what the
  server is doing with a percentage and a thin bar (listing the Drive
  folders 0-5%, reading new invoices 5-75% by files read of files to
  read, the rules 78%, waiting for Gemini 85%, done 100%), a second row
  waits for the first, and a message says what was found.
  AP or the card's owner may use it.
- **Invoice picker** (from *Link...*): every invoice of the surrounding
  month folders, searchable by vendor, file, folder and amount; ones already
  linked elsewhere are greyed out.
- **Add receipt...** replaces *Link...* on the missing purchases of cards
  with receipt upload (`RECEIPT_UPLOAD_CARDS`, today 4421, unless the card's
  own switch in `/admin` says otherwise). The dialog takes
  photos (*Take photo* opens the phone's camera) or files (*Choose photo or
  PDF*; a drop zone on a computer), several photos for a long receipt, shows
  where the file will go with *Change*, and saves with *Save receipt*: the
  file goes to the Invoice folder (above) and is linked to the purchase.
  *Already in Drive? Link a file instead* opens the picker. A dropped
  connection is retried with the same upload id, so a receipt is never
  saved twice. After saving, the dialog shows what the reader found and
  offers the next purchase. Uploaded rows show *Uploaded by* with
  *Remove...*; a disagreement shows *check receipt*.
- **On a phone** (640px and narrower) the table becomes one block per
  purchase: date, description and amount, then the status (*Invoice
  missing*, *Invoice found* with the linked file, *Possible match*) and its
  buttons, then cost centre and use case; a coloured edge (red missing, amber
  possible, green found) shows the status while scrolling. The status
  filters above (*Missing*, *Matched*, ...) work as on a computer.
- **Receipts list** (`#/YYYY/MM?card=4421&view=receipts`, the Chat card's
  *Add receipts* button, or *Receipts list* in the toolbar while that card
  is picked): a phone-sized list of the card's purchases still needing a
  receipt, each with *Take photo* and *Upload*, *No receipt for this
  charge*, possible matches with *Confirm*, uploads to check; a summary
  at the top (*33 missing*, *2 found*, *1 no receipt*) and, below, every
  purchase that has its invoice (*Invoice found*: the file, and whether it
  was found automatically, linked by hand or added from the dashboard) and
  the ones marked as having no receipt (with *Undo*). Someone who may only read the card sees the list with a note to
  ask AP to set them as the owner.
- **Uploads this month** and **Last notifications** lists for AP.

## Admin pages (/admin)

AP's own pages, opened with *Admin* on the dashboard (AP only; see "Access
and roles"). Six tabs down a dark sidebar, or a strip that scrolls sideways
on a phone: *Cards*, *Use cases & cost centres*, *Vendors*, *People*,
*History* and *Settings*. The address names the tab (`/admin#vendors`,
`/admin#history?entity=card&entity_id=4431`), so a tab can be linked. An
editor with unsaved changes asks before it is closed, before another tab
opens and before the page is left. Every change made here, in the
dashboard's *Cards...* dialog and through *New person…* is written to the
History.

**Use cases and cost centres (the November 2026 split).** Until then the
Cost centre list mixed places with kinds of spending. Now:

- a **cost centre** is where a purchase belongs: the seven salons (Dufferin,
  Ridgeway, Consumer, Eglinton, STC, Rapistan, Brampton), US, EU and the new
  *Head office* for online and head-office spending;
- a **use case** is what it was for: the dashboard's Use case box (still the
  `usage` field in the database, the API and the export). The list started
  as the eleven former expense types in their old order: Marketing, IT &
  Software, Shipping & Logistics, Travel, Meals & Entertainment, Office &
  Supplies, Inventory / COGS, Professional Services, Utilities & Telecom,
  Insurance & Government, Other. A use case is for every group or for one
  card group (Hair, Beauty).
- **Uncoded means no use case**, everywhere it is counted. A cost centre
  alone does not code a purchase.
- Applying the schema **retires** the eleven expense types in
  `cost_centers` (never deletes them, and leaves any a purchase carries
  active), once: `app_state` `coding_split` records it, so a later apply
  never retires one AP has restored. A new database is seeded with the
  places only.
- Both lists are edited the same way: add, edit, reorder (↑ ↓, then *Save
  order*), retire and restore; nothing is deleted. A retired name stays on
  the purchases that carry it and in the export, and leaves the dashboard's
  pickers. A rename that changes more than capitals or spaces is refused
  while purchases carry the name (the message gives the count): add the new
  name and retire the old one instead (*Add 'New' and retire 'Old'* does
  both); purchases already coded keep the old name and still count as coded.
  Renaming a cost centre that is a card's default renames the default too.
- Under each list, the values typed on purchases that are not on it, each
  with *Add as use case* or *Add to list*, and the use case names typed into
  the Cost centre box (those purchases still count as having no use case).

**Cards tab.** Three views, *Gallery* (the default), *Table* and *By owner*,
switched in the toolbar and remembered by the browser; the same search and
flag chips drive all three, and the sort the first two (*Card* puts a
supplementary card right after its primary). The **gallery** draws each
card as the card itself, on a fixed template (logo, chip, number, holder and
network sit at the same place on every face): the bank's face (RBC royal
blue with a fine gold double hairline, Chase midnight blue with the white
wordmark and a faint octagon outline, graphite with a dashed *+ Set bank*,
which opens the editor on its Bank field), the product under the logo
(without a trailing network word the mark already shows: *Business Cash
Back*, not *… Mastercard*), the group top right (a frosted pill with a
Hair/Beauty dot), chip and contactless mark, the number as
`•••• •••• •••• 4449` (the card number when it has one, with an *ACCT 4431*
pill beside it), *PRINTED HENRY LI* when the name on the card is someone
else's, *SUPPLEMENTARY OF 4431* for a supplementary card, the holder on the
bottom line, and the network (Visa, Mastercard, AMEX) level with it. A card
with reminders off (`active` false: no weekly Chat card; nothing else
changes) is muted with a *Reminders off* tag under the group. Under each
card, in the same order under every card so a row reads straight across:
activity (*6 invoices missing · last used Nov 2026*, the count a link to the
dashboard's newest month with one missing for that card, or *All invoices
in*; then *41 transactions since Sep 2026*), the owners (in-domain addresses
by name, an outside one in amber, *No owner* in amber when there is none)
with its Chat space when it has one (*own space* or *shared*) and *Edit*,
then *Account 4431 · card no. 4449* when they differ and the supplementary
link (each number brings that card into view), and its flags as amber chips,
those asking for an action first (*No Chat webhook of its own*, nearly every
card's normal state, and *No owner* are left out there, and *No activity in
90 days* when *Never used* says it; their chips still filter). Search also
knows *amex* and *mc*. Sorted by group, the cards come in sections (Hair,
Beauty, No group) with counts. A click on a card, *Edit*, or Enter on a
focused card (one tab stop per card: *Edit* is for the mouse) opens the
editor, whose *On the card* row holds bank, network, product and card
number. The banks and their logos come
from the server (`BANKS` in `main.py`: key, name, short name, logos in
`cloud_run/static/banks/`), so adding a bank is a server change: its key,
names and two logos (for a light background and for the card face), plus
its face colours in `admin.html` (`.cc-face.bank-<key>`; without them the
face is plain graphite).

The **table** has one row per card: bank (logo, product), holder (*not a person* when the holder is
not linked to a person), group, owners (an address outside
`ALLOWED_DOMAINS` is amber: it cannot sign in), default cost centre, Chat
webhook, receipt upload, activity (transactions, those of the last three months,
missing, last month seen), flags and the last change. Chips filter the
cards by flag: *No owner*, *No group*, *Holder not linked to a person*, *No
Chat webhook of its own*, *No activity in 90 days*, *Never used*, *Never
reviewed*, *Default cost centre retired*, *No default cost centre (its
primary card has one)*. *By owner* lists each owner's cards. A click opens
the card's editor, which sends only the fields that changed:

- **Default cost centre** (`cards.default_cost_center`): a salon card is
  that salon. Saving a use case on one of the card's purchases that has no
  cost centre fills it in (dashboard, above). A retired default fills
  nothing until the card gets another. A supplementary card has its own
  default (*Use the same* copies the primary's) because it may be used
  somewhere else. **Fill into coded purchases without a cost centre**
  (offered right after a new default is saved, with the count, and in the
  editor) fills the default into the card's purchases that were given a use
  case before the default existed. It never replaces a cost centre, and one
  set meanwhile is kept.
- **Receipt upload** (`cards.receipt_upload`): *On*, *Off*, or *Follow
  server setting* (empty: `RECEIPT_UPLOAD_CARDS` decides). The card's own
  setting wins, even over `RECEIPT_UPLOAD_CARDS=all`, for the dashboard's
  *Add receipt...*, the receipts list and the Chat card's *Add receipts*,
  in the service and the weekly reminder alike.
- **Bank**, **Network** and **Product** (`cards.bank`, `cards.network`,
  `cards.product`): the card as issued, drawn on its face in the gallery.
  Bank is one of the server's banks (RBC Royal Bank, Chase) or not set;
  Network Visa, Mastercard or American Express; Product the bank's name for
  the card (*Avion Business*, *U.S. Dollar*), at most 60 characters.
- **Chat reminders on** is the card's `active` flag. Webhooks themselves are
  not edited here (Settings says how).
- **Delete card**, for unused cards only: no transactions, not listed on an
  upload, not the primary of another card (the editor says what stops it).
  When removing the card would move reissued receipt numbers to another
  card ("receipts showing 3906 would count as card 3922's"), the page lists
  them and asks again. While an import, a match or the weekly reminder runs,
  the delete answers "an import or match is running; try again in a minute".
  A statement that lists the card again adds it back.
- **Add card** creates a card before its first statement, with its owners,
  default and receipt setting in the same step.

Under the cards, **Numbers on receipts that no card claims**: receipts read
from the Invoice folder that show a card number no card has, so they can
never be matched. *Find the card* lists the cards with a purchase of the
receipt's amount 0-21 days after it; *Use as card number of 4431* gives the
number to that card (a second question when it replaces another number),
then offers *Match Oct 2026 for card 4431* for each month concerned, since
nothing else rematches them. *Ignore…* (with a reason) hides a misread
number; *Restore* brings it back.

**Use cases & cost centres tab.** The two lists side by side, as described
above. Until the schema is applied the tab says what is missing and its
buttons are disabled.

**Vendors tab.** It does **not** change matching or where receipts are
filed: the registry only feeds coding suggestions (Round B, which would let
patterns help matching, comes later and is reviewed and backtested
separately). Three views:

- *Catalogue*: every vendor folder under the Invoice folder, with its
  documents, months, how Gemini read the supplier's name, matched,
  possible, documents nothing links to, purchases missing their invoice that
  start like the folder's matched ones, and a *Needs attention* order. Read
  from whole-table reads kept for 5 minutes; *Refresh* reads again. A
  folder opens its detail: the descriptor starts linked to it (and to other
  folders), its unmatched documents, its proposals, and *Register as vendor*.
- *Registry*: the vendors. A vendor is a name, a kind (supplier, payment
  processor, agent, not a vendor), *code each purchase* for marketplaces
  such as Amazon (no default), its folders (several folders can be one
  vendor; a folder belongs to one vendor), its patterns and its default use
  case per card group. A pattern is how the card descriptor starts, the
  whole descriptor, or the supplier name read on its documents; it is *On
  (suggestions only)* or *Off*. Patterns that would catch other vendors are
  refused: under three characters, a processor or platform word alone (`sq`,
  `paypal`: add the next word) or a one-word start under four letters
  (`air`).
- *Proposals*, worked out from the link history on each read: a descriptor
  start always linked to one folder (three times or more), one start linked
  to two to four folders (one vendor?), and a registered supplier whose
  purchases people coded one way (three or more, three in four the same).
  *Accept* sends the change it describes; *Dismiss* hides it.

**Coding suggestions** (Settings, **off** by default). When on, an uncoded
purchase that someone may edit gets a suggestion when its vendor is found
(an exact descriptor pattern, then the longest start, then its invoice's
folder, then the supplier name on it) and that vendor has a default use
case for the card's group or for any group. Marketplaces and non-suppliers
get none. A suggestion is a button on the dashboard; nothing is written
until someone presses it or picks a value.

**People tab.** Everyone purchases can be assigned to, with the cards they
hold, the cards they own by e-mail, and their purchases. A holder whose
e-mail is not among the card's owners gets *Make owner of 4431*, which asks
first, naming the address (only AP sets a person's e-mail). Edit
renames (refused when the name exists, offering a merge; the cards they
hold follow, and a default label with them) and sets the e-mail, which
gives no rights by itself: owning a card is what lets someone code it.
*Merge into…* moves purchases and cards to another person and deletes this
one. *Delete* says what it would unassign first; a card holder is never
deleted (merge them, or change the card's holder). *Link card holders*
links every card holder to a person, adding the missing ones, and the page
lists names that look like the same person.

**History tab.** The `admin_audit` log, newest first: who (the signed-in
e-mail, or the typed name in open and password mode), when, what changed
(before and after), filterable by kind, id and person, with *Load older*.
Written on a best-effort basis: a change is never refused because its log
row could not be written. **History starts at the deploy** of these pages;
earlier changes are only in the rows' *Updated by*.

**Settings tab.** Read only, apart from the suggestions switch: the sign-in
mode, the AP addresses (`AP_EMAILS`: change the default in `main.py` and
the deploy workflow, then push; a console-only edit is undone by the next
deploy), receipt upload per card, which cards have their own Chat webhook
(Secret Manager `ap-reconciliation-card-webhooks`; a new version reaches
the service on its next deploy or instance), and whether every table and
column the pages need is there, with the date and retired names of the
coding split.

**Data model.** The pages add the tables `use_cases`, `vendors`,
`vendor_folders`, `vendor_patterns`, `vendor_coding` and `admin_audit`, the
columns `cards.default_cost_center` (follows a cost-centre rename) and
`cards.receipt_upload`, `cost_centers.active`, `description` and the
updated stamps, and `people.updated_by`/`updated_at`. People's e-mails and
cost-centre names are unique ignoring case. `app_state` keys:
`coding_split` (the one-time split), `coding_settings` (the suggestions
switch), `vendor_registry` (a version counted up on each registry change),
`vendor_proposals` (dismissed proposals) and `ignored_card_numbers`.

## Chat reminders per card

Each credit card has its own Chat space (the card owner and AP in it), and
each space has an incoming webhook. The webhook URLs are secrets and travel
as one JSON object in `CARD_WEBHOOKS_JSON`:

```json
{"1610": "https://chat.googleapis.com/v1/spaces/AAAA.../messages?key=...&token=...",
 "4894": "https://chat.googleapis.com/v1/spaces/BBBB.../messages?key=...&token=...",
 "default": "https://chat.googleapis.com/v1/spaces/CCCC.../messages?key=...&token=..."}
```

A card without its own entry falls back to `default` (or to
`GCHAT_WEBHOOK_URL`); with neither, the notify result says *no webhook
configured for this card* and nothing is sent. To create a webhook: open the
space in Google Chat, click the space name, **Apps & integrations**,
**Webhooks** (*Add webhooks* / *Manage webhooks*), name it `AP
reconciliation`, save and copy the URL. Cards that are inactive (the flag
under *Cards...*) are never notified.

Each Chat card names the month, the purchases with no invoice (count and
total), the possible matches to confirm and, when there are any, **Without
use case: n** (the card's purchases with an empty Use case box). Its footer
asks for the receipts, and to open the dashboard to confirm possible
matches, "say what each purchase was for (its use case)" or mark a charge as
having no invoice. **Reminders are about receipts:** a card whose receipts
are all in gets no reminder even when some of its purchases still lack a
use case (its notify result stays `nothing missing`). Coding progress is
the dashboard's *No use case* tile and filter.

On cards with receipt upload (`RECEIPT_UPLOAD_CARDS`, today 4421, or the
card's own switch in `/admin`) the card's
*Invoice folder* button is replaced by **Add receipts**, first and blue,
linking to `<dashboard>/receipts/<last4>/<YYYY>/<MM>`. That path (not a
`#` link, which a Google sign-in on the way can drop) redirects to the
receipts list, so the card holder can photograph each receipt from the
phone. Chat cards posted before keep their old button until the next send.

## Endpoints

*Auth* is what the app checks: behind IAP (the live deployment) "signed in" means a valid assertion for an allowed account; in password mode it means the session cookie a `POST /api/login` set, and every caller is `ap`; in open mode it means anyone.

| Method and path | Auth | What it does |
|---|---|---|
| `GET /` | none | the dashboard page |
| `GET /admin` | none (IAP guards the path, as for `/`) | the admin pages; the page holds no data, every `/api/admin` call checks the role |
| `GET /static/banks/<name>.svg` | none (IAP guards the path, as for `/`) | the bank logos the Cards gallery draws (`BANKS`); kept by the browser a day (`Cache-Control: private, max-age=86400`) and served with `Content-Security-Policy: default-src 'none'` |
| `GET /health` | none | `{"ok": true, "store": ..., "last_job": ...}`; `?deep=1` adds reachability checks for Supabase and the Invoice folder, how many card webhooks are configured (counts only: it answers without the password) and the auth mode. (`/healthz` is answered by Cloud Run's front end itself, not the container.) |
| `GET /api/config` | none | `auth` (`password`, `open` or `iap`), `auth_disabled`, dashboard URL, sign-in page URL (`login_url`), Invoice folder URL, `ai_enabled` |
| `POST /api/login` | none, but `X-Requested-With: XMLHttpRequest` in password mode (403 without) | password mode: `{"password": "..."}` sets the session cookie (401 wrong, 429 after too many wrong ones); a no-op in the other modes |
| `POST /api/logout` | none, but `X-Requested-With: XMLHttpRequest` in every mode (403 without) | clears the session cookie (*Lock*) |
| `GET /api/me` | signed in | e-mail, name, role, cards owned |
| `GET /api/months` | signed in | month navigation with per-card counts (`uncoded`: purchases without a use case), cards, cost centres (active ones), `use_cases` (active ones: `id`, `name`, `group_name`, `description`, in AP's order), people |
| `GET /api/transactions?year&month[&card]` | signed in | the month's transactions with their invoice, `editable` flag, `person_id`, the people list, uploads and recent notifications, plus `use_cases` (as above) and `suggestions` (the switch); while it is on, an editable purchase without a use case whose vendor has a default carries `suggestion` (`usage`; `cost_center`, the card's active default when the purchase has none; `vendor`; `source`, what suggested it) |
| `PATCH /api/transactions/<id>` | signed in, AP or the card's owner | body: `cost_center`, `usage` (the use case), `note`, `person_id` (a person's id, or `""` to clear; 400 for one that does not exist), and/or `action` (`waive` with `reason`, `unwaive`, `confirm`, `link` with `invoice_id`, `unlink`). A cost centre or use case typed with other capitals or spaces is stored with the list's spelling; anything else is stored as typed. Setting a use case on a purchase with no cost centre, without sending `cost_center`, fills the card's active default cost centre and answers `filled_cost_center`; if someone set a cost centre meanwhile, the use case is still saved, that cost centre is kept and no `filled_cost_center` is returned. A 409 *This purchase changed meanwhile; reload.* comes only when the request's own conditions failed (a Confirm whose proposed invoice moved, say). A use case name sent as `cost_center` on a row without a use case answers a `hint` |
| `GET /api/people` | signed in | `{"people": [{"id", "name", "email"}]}`, by name |
| `POST /api/people` | signed in | `{"name"[, "email"]}`: adds a person (`201 {"person", "created": true}`, recorded in the History), or answers `200` with the one already named so (case and extra spaces aside); 400 for an empty name, one over 100 characters or a bad e-mail; 403 for an e-mail sent by a member (only AP sets one: People offers *Make owner* from it); 409 for an e-mail another person has |
| `POST /api/ask?year&month[&card]` | signed in | voice mode: `{"question": "..."}` (up to 500 characters; `card` is four digits) answered as `{"answer", "source": "gemini" \| "summary"}` for the browser to read aloud; read only |
| `POST /api/transactions/<id>/rematch` | signed in, AP or the card's owner | *Rematch*: reads new invoices for the purchase's months, then looks for its invoice again, as a background job (202 with the job, polled through `/api/jobs/current?id=`; 409 while another job runs; 400 for a waived, non-purchase or hand-linked row). The job's result has `outcome` (`found`, `same`, `kept`, `none` or `conflict`), the status and invoice the row now has, `changed`, `invoices_read`, `read_errors` with `read_error_files`, and the updated row; its progress has `stage` and `percent` |
| `GET /api/transactions/<id>` | signed in | one row as the table shows it (with `checks` and `invoice.reading`); the receipt dialog polls it while the file is read |
| `GET /api/transactions/<id>/receipt-target` | signed in, AP or the card's owner, card with receipt upload | where *Save receipt* would put the file: `month_folder`, `month_exists`, `vendor_folder`, `vendor_exists`, `source` (`this_month`, `earlier_month` or `new`), the month's `folders`, `limits`. Creates nothing |
| `POST /api/transactions/<id>/receipt` | signed in, AP or the card's owner, card with receipt upload | multipart: one to ten `file` parts (photos; or one PDF), optional `vendor_folder` and `upload_id`. Saves to Drive and links the purchase: `201 {"status": "saved", "file", "transaction", "reading"}`; the same `upload_id` again answers `200` with `"replayed": true`, also when it arrives while the first is still saving (its own copy is trashed); `409 not_saved` when the purchase already has its invoice, is waived or changed meanwhile (the file is then trashed); 413 too large (a body over 16 MB is refused before it is read), 415 not a photo or PDF, 429 more than 40 in ten minutes |
| `DELETE /api/transactions/<id>/receipt` | signed in, AP or the card's owner | *Remove...*: unlinks a receipt added from the dashboard and sends its file to the Drive trash; the file stays out of matching even when the trash fails (`200` with `"trashed": false` and a `warning`) |
| `GET /receipts/<last4>/<YYYY>/<MM>` | signed in | the Chat card's *Add receipts*: redirects to the receipts list |
| `GET /api/invoices?year&month` | signed in | invoices of month-1..month+1 with a `linked` flag (the picker) |
| `GET /api/cards` | signed in | cards, each with `default_cost_center` (`""` for none), `receipt_upload` (whether it is on), `receipt_upload_setting` (the card's own `true`/`false`, or `null` to follow `RECEIPT_UPLOAD_CARDS`), and `bank`, `network`, `product` (`""` when not set) |
| `POST /api/cards` | AP | `{"last4", "label", "holder_name", "group_name", "card_number", "primary_last4", "name_on_card", "owner_email", "chat_space", "active"}`; `owner_email` may list several addresses (commas, semicolons or spaces), stored lower-case as `a@x, b@x`; 400 if one is not an address, a card number is not four digits, is another statement card or is already another card's, or a primary card is the card itself, not on the dashboard, or itself supplementary or a primary already. A new holder is added as a person (`cards.person_id`), only once nothing in the save was refused. An unknown `last4` answers 404 unless the body has `"create": true` (409 if that card exists); a create that would take reissued receipt numbers from another card answers 409 with `alias_changes` (`number`, `before`, `after`) unless `"confirm": true`. `expect_card_number` (optional; `""` for none) refuses with 409 when the card's number is no longer that, so a page cannot replace a number it has not seen. `default_cost_center` (an active cost centre, snapped to its spelling; `""` or `null` clears; else 400) and `receipt_upload` (`true`, `false` or `null`; else 400). `bank` (a key of `BANKS`: `rbc`, `chase`; case ignored), `network` (`visa`, `mastercard`, `amex`; case ignored) and `product` (text, spaces tidied, cut at 60 characters): `""` or `null` clears each, anything else answers 400 (`bank: ...`, `network: ...`, `product: ...`); they work on a create too. 400 for a body that is not a JSON object. Answers the cards, the people, `card` (the saved card), and `fillable` (the purchases with a use case and no cost centre the new default could fill) when an active default was set. Each create and change is recorded in the History |
| `GET /api/export/missing` | signed in | Excel file of the purchases still missing their invoice (the use case column is headed *Use case*, formerly *Usage*); `?group=Hair` (`-` for the cards in no group), `?card=4431`, `?year=&month=` for one month (else every month); 400 for a bad card or month. Totals per group and currency, never added across currencies. Header `X-Rows`: how many |
| `GET /api/statements?year&month` | signed in | uploads that touched the month |
| `POST /api/statements[?background=1]` | AP | multipart `file`; `?sync=1` also indexes, matches and notifies (`&notify=0` to skip the cards) |
| `POST /api/index?year&month[&limit]` | AP | read up to `limit` (max 100) new invoices for month-1..month+1 |
| `POST /api/match?year&month[&card]` | AP | pair purchases with invoices |
| `POST /api/notify?year&month[&card][&dry_run=1]` | AP | post the Chat cards (`dry_run` returns the payloads instead) |
| `POST /api/jobs?kind=index\|match\|notify&year&month[&card][&limit=8]` | AP | the same step as a background job, answered at once with `202 {status: started, job}` (`409 busy` while another runs). What the dashboard uses: an invoice read or a match can run for minutes and office networks drop a silent response after a minute or so. `index` loops over batches of `limit` files until nothing new is left |
| `GET /api/jobs/current[?id=…]` | signed in | the job with that id (the last ten stay readable, so a tab keeps its own result when someone else starts the next job; 404 after a restart), else the newest: `id`, `name`, `started`, `finished`, `running`, `progress` (`read`, `live`, `remaining`, `note`, `year`, `month`, …) and, once finished, `result` or `error` |
| `GET /api/admin/settings` | AP | what the Settings tab shows: auth mode, store, AI, allowed domains, `ap_emails` and how to change them, receipt upload (`env`, `all`, `per_card`), webhooks (which cards have their own; no URLs), `schema` (each table and column the pages need: `true` when present; `coding_split`), `coding_split`, `coding` (`suggestions`, `by`, `at`), `registry_version`; `schema_error` when the check itself failed |
| `PUT /api/admin/settings/coding` | AP | `{"suggestions": true\|false}`: the coding-suggestions switch (400 for anything else) |
| `GET /api/admin/cards` | AP | the Cards tab: every card as in `/api/cards` plus `activity` (`transactions`, `recent`, `missing`, `first_month`, `last_month`, and `missing_last_month`: the newest month with an invoice missing), `flags`, `owners`, `last_notification`, `last_change`, `default_cost_center_active`, `primary_default`, `deletable` and `delete_blockers`; `counts` per flag, `flag_labels`, `banks` (`[{key, name, short, logo, logo_on_dark}]`, logos under `/static/banks/`), `networks`, groups, people, active cost centres, `owners` (cards per owner address), `unclaimed` (receipt card numbers no card claims, with `ignored`) |
| `DELETE /api/admin/cards/<last4>[?confirm=1]` | AP | deletes an unused card: 409 with `counts` when it has transactions, an upload lists it or it is a primary card; 409 with `alias_changes` when receipt numbers would move to another card, until `?confirm=1`; 409 "an import or match is running" while a job holds the lease or runs in this instance; 400 for a `last4` that is not four digits, 404 unknown |
| `POST /api/admin/cards/<last4>/fill-default` | AP | fills the card's active default cost centre into its purchases with a use case and no cost centre (never over one set meanwhile): `{"filled", "cost_center"}`; 400 when the card has no active default |
| `GET /api/admin/card-numbers/<n>` | AP | the receipts showing card number `n` and the likely cards (purchases of the receipt's amount 0-21 days later; each with `card_number`, `card_number_receipts`, `months`), `considered`, `ignored`; 404 when no receipt shows it |
| `POST\|DELETE /api/admin/card-numbers/<n>/ignore` | AP | POST `{"reason"}` (1-200 characters) hides a misread number from the panel; DELETE brings it back (404 when not ignored) |
| `GET /api/admin/coding` | AP | every cost centre (with `uses`, `card_defaults`) and use case (with `uses`), retired ones included; `usage_not_listed`, `cost_centers_not_listed`, `cost_centers_like_use_cases`, `groups`, `schema_missing` |
| `POST /api/admin/cost-centers` | AP | `{"name", "sort"?, "description"?}`: 201; 409 with `existing` (`name`, `active`) when the name exists ignoring case |
| `PATCH /api/admin/cost-centers?name=<current name>` | AP | `{"name"?, "sort"?, "description"?, "active"?}`: a rename beyond capitals and spaces is 409 with `uses` while purchases carry the name; a rename carries over to cards' defaults; retiring answers `warnings` naming the cards it was the default of |
| `POST /api/admin/cost-centers/order` | AP | `{"names": [...]}` in the new order (names not sent keep their place after them; unknown or repeated ones are 400) |
| `POST /api/admin/use-cases` | AP | `{"name", "group_name"?, "description"?, "sort"?}`: 201, placed before *Other* when no sort is given; 400 for a group no card has; 409 with `existing` (`id`, `name`, `active`) |
| `PATCH /api/admin/use-cases/<id>` | AP | `{"name"?, "group_name"?, "description"?, "sort"?, "active"?}`: the same rename rule (409 with `uses`); retiring answers `warnings` naming vendors that suggest it |
| `POST /api/admin/use-cases/order` | AP | `{"ids": [...]}` in the new order, as for cost centres |
| `GET /api/admin/vendors?q&sort&registered&page&per[&fresh=1]` | AP | the folder catalogue, a page at a time (`sort`: `attention`, `volume`, `name`; `registered`: `all`, `yes`, `no`), with `generated_at` and `registry_available`. Whole-table reads are reused for 5 minutes; `fresh=1` reads again |
| `GET /api/admin/vendors/folder?name=<folder>` | AP | one folder: its descriptor starts (each with `problem` and `owner`), unmatched documents, vendor and `proposals`; 400 without a name, 404 for a folder with no documents |
| `GET /api/admin/registry` | AP | `available`, `version`, `vendors` (each with `folders`, `patterns`, `coding` and `resolves`), active `use_cases`, `groups`, `kinds`, `folder_names` |
| `POST /api/admin/vendors` | AP | `{"name", "kind"?, "code_each"?, "note"?, "folders"?, "patterns"?, "source"?}`: 201 `{"vendor"}`; 409 with `clash` (`name`, `folder` or `pattern`), `value` and `existing` (`id`, `name`); 400 for a pattern that would catch other vendors |
| `PATCH /api/admin/vendors/<id>` | AP | `{"name"?, "kind"?, "code_each"?, "active"?, "note"?}`: `{"vendor", "warnings"}` (coding defaults that a marketplace or non-supplier no longer uses) |
| `POST /api/admin/vendors/<id>/folders`, `DELETE /api/admin/vendor-folders/<id>` | AP | add `{"folder"}` or `{"folders": [...]}` (201; 200 when the vendor already holds them all; 409 when another vendor holds one), or remove one |
| `POST /api/admin/vendors/<id>/patterns`, `PATCH\|DELETE /api/admin/vendor-patterns/<id>` | AP | add `{"pattern", "kind"?: "descriptor"\|"invoice_vendor", "match_type"?: "prefix"\|"exact"}` (a supplier name is always exact; new patterns start on), set `{"status": "probation"\|"off"}` (shown as On and Off; matching does not use patterns, so `trusted` is 400), or remove one |
| `PUT /api/admin/vendors/<id>/coding` | AP | `{"group_name": ""\|group, "use_case_id": id\|null}`: the vendor's default use case for that group (or any group); 400 for a marketplace, a non-supplier, a group no card is in, or a use case not active for that group. `null` removes it, and always may: a default kept from before the vendor became a marketplace or a non-supplier, or from a group that lost its cards, is not used, and the vendor editor offers *Remove* for it |
| `GET /api/admin/proposals`, `POST /api/admin/proposals/dismiss` | AP | the proposals from link history, each with `text`, `evidence`, `effect` and the `accept` request (`method`, `path`, `body`); dismiss takes `{"id", "undo"?}` |
| `GET /api/admin/people` | AP | everyone with `cards_held`, `cards_owned` (by e-mail), `cannot_code`, `purchases`; `holders_unlinked`; `duplicates` (pairs that look like the same person) |
| `PATCH /api/admin/people/<id>` | AP | `{"name"?, "email"?}`: a rename the cards held follow (409 with `merge_into` when the name exists); the e-mail is stored lower-case, `""` clears it, 409 when another person has it |
| `POST /api/admin/people/<id>/merge` | AP | `{"into": id}`: moves purchases and held cards, deletes this person, copies the e-mail when the target has none |
| `DELETE /api/admin/people/<id>[?confirm=1]` | AP | 409 with `holds_cards` for a card holder (always); 409 with `counts` for someone assigned to purchases, until `?confirm=1` |
| `POST /api/admin/people/link-holders` | AP | links every card holder to a person, adding missing ones: `linked`, `duplicates`, `people` |
| `GET /api/admin/audit?entity&entity_id&actor&before_id&limit` | AP | History entries newest first (`limit` 1-200, default 50), `next_before_id`, `entities`; `available: false` while the table is missing |

Job endpoints answer `409 {"status": "busy"}` while another job runs and
`500 {"status": "error"}` with the message when one fails.

Every `/api/admin` call answers 401 when not signed in and 403 for a member. Its writes (POST, PATCH, PUT,
DELETE) also need `X-Requested-With`, are refused with 403 in open mode against the real database, and
are recorded in the History. A uuid id that is not one answers 404. Errors are `{"error": "..."}`; one
about a field starts with its name and a colon (`default_cost_center: not an active cost centre`). A
table or column the database does not have yet answers 502 with `"schema_missing": true` (see
Troubleshooting); any other database failure (a timeout, a 5xx) answers 502 `database error: …`, which a
retry may clear. A duplicate the database refused answers 409.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `STORE` | `supabase` | `memory` keeps everything in-process (tests, demos) |
| `SUPABASE_URL` | | `https://bkkifrwfgtcnmfynbbdd.supabase.co`, set in the deploy workflow |
| `SUPABASE_SERVICE_KEY` | | service-role key; secret |
| `INVOICE_FOLDER_ID` | the Invoice folder above | root of the walk |
| `STATEMENTS_FOLDER_ID` | empty | when set, uploads are copied to Drive under `<folder>/<YYYY MM>/` |
| `BANK_FOLDER_ID` | empty: no Drive import | the folder whose statements `python main.py sync` imports (the Bank Transactions folder, `1MGenhujsY-zElpg6A0SGD8C1WqC516t3`); the workflow sets it on the service (for the dashboard's line) and the sync job |
| `BANK_SETTLE_MINUTES` | `10` | a file modified more recently than this waits for the next run |
| `INVOICE_WATCH` | `1` | `0`: the import run does not read and match invoices newly filed under the Invoice folder (pipeline step 7) |
| `GOOGLE_WORKSPACE_AP_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` | | the AP user's OAuth triple for Drive; secrets. ADC when unset |
| `CARD_WEBHOOKS_JSON` | | the webhook object above; secret |
| `GCHAT_WEBHOOK_URL` | empty | optional single fallback webhook |
| `IAP_AUDIENCE` | | IAP mode only: what the assertion's audience must be, `/projects/304363458561/locations/us-central1/services/ap-reconciliation`; required on Cloud Run when `AUTH_DISABLED` is not set |
| `ALLOWED_DOMAINS` | `superhairpieces.com` | comma-separated e-mail domains allowed in, on top of IAP's access list |
| `AP_EMAILS` | `ap@superhairpieces.com,manne@superhairpieces.com,snow@superhairpieces.com` | addresses with the `ap` role; the workflow sets the same list, so change both |
| `AUTH_DISABLED` | | `1` turns Google sign-in off and makes every caller `ap`: with `DASHBOARD_PASSWORD` the page asks for that password first (password mode), without it the dashboard is open to anyone who can reach it (the 2026-10-12 decision; local runs) |
| `DASHBOARD_PASSWORD` | | the shared password (Secret Manager `ap-reconciliation-dashboard-password`); surrounding whitespace is ignored. Only takes effect with `AUTH_DISABLED=1` (the weekly job gets it through the shared `SECRETS` list and ignores it). Set but blank, the service refuses to start rather than open up |
| `DASHBOARD_SESSION_DAYS` | `30` | how long the password cookie lasts |
| `DASHBOARD_URL` | request origin | absolute URL for the Chat card buttons; the workflow sets it |
| `LOGIN_URL` | empty: *Sign out* is hidden, *Switch account* remains | the public sign-in page, where *Sign out* lands (IAP mode); the workflow sets it after each deploy |
| `GCP_PROJECT` / `VERTEX_LOCATION` | `shp-ai-bot-2026` / `us-central1` | where Gemini runs (ADC) |
| `GEMINI_EXTRACT_MODEL` / `GEMINI_MATCH_MODEL` | see `main.py` | the reading and judging models |
| `AI_DISABLED` | | `1` skips every model call: no invoice reading, rules only, no PDF statements |
| `INDEX_BATCH` / `INDEX_WORKERS` | `30` / `4` | files per index call and download threads |
| `RECEIPT_UPLOAD_CARDS` | `4421` | cards (comma-separated last four digits, or `all`) whose rows get *Add receipt...* and whose Chat card gets *Add receipts*. The default lives in `main.py`, so the service and the weekly job agree; when setting it in the workflow, set it on both. A card's own *Receipt upload* setting in `/admin` (Cards tab: On or Off) wins over this list, `all` included; *Follow server setting* leaves it to the list |

## Secrets

All on `shp-ai-bot-2026`. The runtime identity is the project's default
compute service account, `304363458561-compute@developer.gserviceaccount.com`,
which must hold `roles/secretmanager.secretAccessor` on **each** of these
(per secret, never project-wide; the deploy fails with *Permission denied on
secret* otherwise):

| Secret Manager id | Container env var | Notes |
|---|---|---|
| `ap-reconciliation-supabase-service-key` | `SUPABASE_SERVICE_KEY` | service-role key of the AP Supabase project; bypasses RLS, server side only |
| `google-workspace-ap-client-id` | `GOOGLE_WORKSPACE_AP_CLIENT_ID` | already exists: the OAuth client that issued the AP user's refresh token |
| `google-workspace-ap-client-secret` | `GOOGLE_WORKSPACE_AP_CLIENT_SECRET` | already exists; the three stay together |
| `google-workspace-ap-refresh-token` | `GOOGLE_WORKSPACE_AP_REFRESH_TOKEN` | already exists: `ap@superhairpieces.com`'s refresh token with the `gmail.modify` and `drive` scopes. This service needs only Drive, but the invoice filer in `mannetsang/ap-automation` reads ap@'s Gmail with the same token |
| `ap-reconciliation-card-webhooks` | `CARD_WEBHOOKS_JSON` | the JSON object of Chat webhooks |
| `ap-reconciliation-dashboard-password` | `DASHBOARD_PASSWORD` | password mode only; not mounted while IAP is on (the workflow's `SECRETS` omits it). A new version changes it on the next instance and signs everyone out |
| `SUPABASE_ACCESS_TOKEN` | (not mounted) | Supabase personal access token for `setup_db.py` only; optional, a local `.env` entry does the same |

Gemini (Vertex AI) runs as the compute service account through Application
Default Credentials, as it does for `gbp-reviews`; no secret is involved.
`ap-reconciliation-api-token` is no longer used (the weekly reminder is a
Cloud Run job, not a token-protected endpoint) and can be deleted.
Local `.env` names are the env vars above.

## One-time setup

Commands are for Windows `cmd` (one line each, `python` not `python3`).
A secret value is typed (right-click to paste) at a hidden prompt and piped
straight into `gcloud`, stripped of whitespace, so no file is written and no
trailing newline ends up inside the value (a newline inside a key breaks the
HTTP headers it is sent in). Never paste a value into a chat, a ticket or
this repo.

1. **Supabase project.** Done 2026-10-09: project ref `bkkifrwfgtcnmfynbbdd`
   (<https://supabase.com/dashboard/project/bkkifrwfgtcnmfynbbdd>), URL
   `https://bkkifrwfgtcnmfynbbdd.supabase.co`. For a replacement project:
   create it at <https://supabase.com/dashboard/projects> in the company
   organisation, region *East US (North Virginia)* (closest to `us-central1`),
   keep the database password in the password manager (nothing here uses it),
   and note the **project ref** (Project Settings, General).
2. **Schema.** Applied to `bkkifrwfgtcnmfynbbdd` on 2026-10-09 (23 statements);
   re-applied 2026-11-18 for orders charged in parts (42 statements); to be
   re-applied for the admin pages (128 statements; see the end of this step).
   To re-apply after a schema change: the Management API token is Secret
   Manager `SUPABASE_ACCESS_TOKEN` (or a personal token from avatar, *Account*,
   *Access Tokens*, in the gitignored root `.env` as `SUPABASE_ACCESS_TOKEN=...`), then
   `python ap-reconciliation\setup_db.py --project-ref bkkifrwfgtcnmfynbbdd`
   and expect `ok: applied 128 statements from schema.sql to project bkkifrwfgtcnmfynbbdd`
   (`--dry-run` first prints the SQL and the same count without calling
   Supabase). The count is of lines ending in `;`, so it includes the lines
   inside the `do` blocks.
   The Table Editor now shows `cards`, `statements`, `invoices`,
   `transactions`, `notifications`, `cost_centers`, `people`, `use_cases`,
   the `vendor*` tables, `admin_audit` and the `month_summary` view. Delete
   the token from `.env` (or keep it for future schema changes).

   **The admin pages (November 2026) need it applied again, 128
   statements, before their code is pushed**: `CARD_FIELDS` gained
   `default_cost_center` and `receipt_upload`, so the new code cannot save a
   card on the old schema. In this order, from the repo root:
   `python ap-reconciliation\setup_db.py --project-ref bkkifrwfgtcnmfynbbdd --dry-run`
   then
   `python ap-reconciliation\setup_db.py --project-ref bkkifrwfgtcnmfynbbdd`
   then push. If the apply stops with `schema.sql not applied: two people
   share the e-mail …` or `… cost centres differ only in capitals …`,
   nothing of the admin pages was applied (the check runs first, and the
   request is one transaction): fix the named rows in the Supabase Table
   Editor (give one person another e-mail or none; rename one cost centre)
   and run it again; every statement can be run twice. Between the apply and
   the deploy, the old dashboard still lists the retired expense types in its
   Cost centre picker, and its *No cost centre* tile (cost centres) no
   longer agrees with the server's month counts (use cases): deploy right
   after. After the deploy,
   `/admin#settings` shows every schema item present and the coding split
   done with 11 retired names, `/api/months` lists the 10 places as cost
   centres, and a card save shows a row in the History.
3. **Service-role key.** The secret `ap-reconciliation-supabase-service-key`
   exists (created 2026-10-09). Project Settings, *API Keys*, reveal
   `service_role`, then run the line below and paste the key at the prompt:
   `python -c "import getpass,sys; sys.stdout.write(getpass.getpass('Paste the service_role key, then Enter: ').strip())" | gcloud secrets versions add ap-reconciliation-supabase-service-key --data-file=- --project=shp-ai-bot-2026`
   Rotating the key later is the same line again (scripts read `latest`).
   Version 1 was added on 2026-10-10 straight from the Management API. The
   project URL is not a secret and sits in the deploy workflow's `env`.
4. **Chat spaces and webhooks.** For each credit card create a space (card
   owner + AP, e.g. `AP - Visa 1610`), add an incoming webhook as described
   above and collect the URLs in Notepad as the JSON object shown above,
   on ONE line. Version 2 of the secret (2026-10-09) holds the webhooks of
   cards 1610, 2887 and 4421; add a new version with the full object whenever
   a card is added, pasting the one-line JSON at the prompt:
   `python -c "import getpass,sys,json; sys.stdout.write(json.dumps(json.loads(getpass.getpass('Paste the webhook JSON, then Enter: '))))" | gcloud secrets versions add ap-reconciliation-card-webhooks --data-file=- --project=shp-ai-bot-2026`
   (the `json.loads` rejects a typo before anything is stored).
5. **Access.** The live deployment uses Google sign-in: the workflow
   deploys with `--no-allow-unauthenticated --iap` and
   `IAP_AUDIENCE=/projects/304363458561/locations/us-central1/services/ap-reconciliation`
   (decided 2026-10-13; no button or popup for people already signed in to
   Google). The one-time grants are in place (the IAP API is enabled, IAP's
   service agent may invoke the service, and the `superhairpieces.com`
   domain holds `roles/iap.httpsResourceAccessor` on the service, granted
   by an owner on 2026-10-12); for a fresh project they are, by an owner:
   `gcloud services enable iap.googleapis.com --project=shp-ai-bot-2026`
   `gcloud beta services identity create --service=iap.googleapis.com --project=shp-ai-bot-2026`
   `gcloud run services add-iam-policy-binding ap-reconciliation --region=us-central1 --project=shp-ai-bot-2026 --member="serviceAccount:service-304363458561@gcp-sa-iap.iam.gserviceaccount.com" --role="roles/run.invoker"`
   `gcloud iap web add-iam-policy-binding --project=shp-ai-bot-2026 --resource-type=cloud-run --region=us-central1 --service=ap-reconciliation --member="domain:superhairpieces.com" --role="roles/iap.httpsResourceAccessor"`
   (the last one only after a deploy has enabled IAP on the service; a
   single colleague outside the domain gets a `user:` grant the same way).
   The app then applies `ALLOWED_DOMAINS` and `AP_EMAILS` on top of IAP's
   list. To open the dashboard instead (no sign-in; the setting from
   2026-10-12 to 2026-10-13), change the workflow to
   `--allow-unauthenticated --no-iap` and replace `IAP_AUDIENCE=...` in its
   `--set-env-vars` with `AUTH_DISABLED=1`, then push (the sign-in page
   then has no job: its buttons lead to IAP paths the dashboard no longer
   has; delete it with
   `gcloud run services delete ap-reconciliation-login --region us-central1 --project shp-ai-bot-2026`
   and remove both of the workflow's sign-in page steps, *Deploy the sign-in
   page* and *Point LOGIN_URL at a new sign-in page*, or leave the service
   and both steps until IAP returns); to put a shared
   password in front of that, also add
   `DASHBOARD_PASSWORD=ap-reconciliation-dashboard-password:latest` to
   `SECRETS` (the secret exists, with step 6's grant). To change that
   password, add a version (typed at a hidden prompt, so it lands in no
   shell history) and redeploy, or wait for the next instance; everyone is
   signed out:
   `python -c "import getpass,sys; sys.stdout.write(getpass.getpass('New dashboard password: ').strip())" | gcloud secrets versions add ap-reconciliation-dashboard-password --data-file=- --project=shp-ai-bot-2026`
   Mounted but blank, say from an empty secret version, the service refuses
   to start instead of opening up; the revision's log says
   `DASHBOARD_PASSWORD is set but blank`.
6. **IAM grants.** One per secret, to the compute service account; all in
   place (the webhooks, API token and dashboard-password secrets were
   granted when they were created, the Supabase key and the three AP Drive
   secrets before the first deploy on 2026-10-12). For a fresh project, one
   line per secret:
   `gcloud secrets add-iam-policy-binding ap-reconciliation-dashboard-password --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding ap-reconciliation-card-webhooks --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding ap-reconciliation-supabase-service-key --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding google-workspace-ap-client-id --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding google-workspace-ap-client-secret --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   `gcloud secrets add-iam-policy-binding google-workspace-ap-refresh-token --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=shp-ai-bot-2026`
   The compute account normally carries Editor, which covers Vertex AI; if
   that was trimmed, add `roles/aiplatform.user` on the project.
7. **First deploy.** Done 2026-10-12: the dashboard answers at
   `https://ap-reconciliation-onvg62bzra-uc.a.run.app` (behind IAP since
   2026-10-13) and the sign-in page, the address to share, at
   `https://ap-reconciliation-login-onvg62bzra-uc.a.run.app`. Later
   deploys happen on every push to `main` touching `cloud_run/` or
   `login/`, or from the Actions tab (*Deploy AP credit-card reconciliation
   service*). The URLs are also printed by
   `gcloud run services describe ap-reconciliation --region us-central1 --project shp-ai-bot-2026 --format "value(status.url)"`
   and the same with `ap-reconciliation-login`.
8. **Weekly reminder.** The deploy also creates the Cloud Run job
   `ap-reconciliation-remind` (`python main.py remind` in the service's
   image, same secrets: for each card, the newest month that still has
   missing or possible invoices is re-indexed, re-matched and notified
   again). Cloud Scheduler starts it on Mondays 14:00 UTC through the Cloud
   Run API as the compute service account, so no token and no path through
   IAP are involved; the scheduler job is created once by an owner (done
   2026-11-10; first run Monday 2026-11-16):
   `gcloud scheduler jobs create http ap-reconciliation-remind --location=us-central1 --project=shp-ai-bot-2026 --schedule="0 14 * * 1" --time-zone=Etc/UTC --uri="https://run.googleapis.com/v2/projects/shp-ai-bot-2026/locations/us-central1/jobs/ap-reconciliation-remind:run" --http-method=POST --oauth-service-account-email=304363458561-compute@developer.gserviceaccount.com`
   Test it with
   `gcloud scheduler jobs run ap-reconciliation-remind --location=us-central1 --project=shp-ai-bot-2026`
   then
   `gcloud run jobs executions list --job=ap-reconciliation-remind --region=us-central1 --project=shp-ai-bot-2026`
   and the execution's logs. A 403 on the run means the compute account may
   not start the job:
   `gcloud run jobs add-iam-policy-binding ap-reconciliation-remind --region=us-central1 --project=shp-ai-bot-2026 --member="serviceAccount:304363458561-compute@developer.gserviceaccount.com" --role="roles/run.invoker"`
   The job can also be started by hand:
   `gcloud run jobs execute ap-reconciliation-remind --region=us-central1 --project=shp-ai-bot-2026`
9. **Drive import.** The schema (step 2) is applied again **before** the
   code that uses it deploys: `statement_sources`, `job_lease` and
   `app_state` (applied 2026-11-02, 30 statements). Every long step takes the lease in
   `job_lease`, the weekly reminder included, so without the table it fails.
   Then two things, once: the **Bank Transactions** folder is shared with
   `ap@superhairpieces.com` as a viewer (its owner, nikki@, does this:
   editors may not share it), and Cloud Scheduler starts the job the deploy
   creates, every 15 minutes, the same way as the weekly one:
   `gcloud scheduler jobs create http ap-reconciliation-sync --location=us-central1 --project=shp-ai-bot-2026 --schedule="*/15 * * * *" --time-zone=Etc/UTC --uri="https://run.googleapis.com/v2/projects/shp-ai-bot-2026/locations/us-central1/jobs/ap-reconciliation-sync:run" --http-method=POST --oauth-service-account-email=304363458561-compute@developer.gserviceaccount.com`
   Until the folder is shared the dashboard's line under the upload box says
   so, and each run ends in seconds.
   On 2026-11-02 the lines uploaded by hand before these parser fixes were
   corrected in place (936 lines nobody had touched: Chase 4516/4692/9783
   from CAD to USD, the True Top 4805/5018 lines Gemini had read with their
   signs turned round, bank fees typed as fees, `&amp;` decoded), and their
   months were queued for matching; the import recognises those lines, so
   the folder's copies of the same statements add nothing twice. Run it by hand with
   `gcloud run jobs execute ap-reconciliation-sync --region=us-central1 --project=shp-ai-bot-2026`.
10. **Cards.** Open the dashboard as AP, upload the first export, then under
   *Cards...* give each card its label, owner e-mail (the person who may
   code its charges) and Chat space name. The *Webhook* column shows
   whether the card has an entry in `CARD_WEBHOOKS_JSON`.

## Local runs

Install the runtime once: `pip install -r ap-reconciliation\cloud_run\requirements.txt`.
Everything reads the gitignored root `.env` through `lib/secrets.py`, so no
variable has to be exported by hand.

**Demo without a database or Google.** From `ap-reconciliation\cloud_run`:

```
set STORE=memory&& set AUTH_DISABLED=1&& set AI_DISABLED=1&& python main.py serve --port 8765
```

(bash: `STORE=memory AUTH_DISABLED=1 AI_DISABLED=1 python main.py serve --port 8765`).
Then upload an export and open <http://127.0.0.1:8765>:

```
curl -F "file=@C:\path\to\export.xlsx" "http://127.0.0.1:8765/api/statements"
curl "http://127.0.0.1:8765/api/months"
curl "http://127.0.0.1:8765/api/transactions?year=2026&month=9"
```

Every row is `missing` (no invoices are read with `AI_DISABLED`), every row
is editable, and the cost centre, use case, waive and link actions all work
against memory. Restarting the server forgets everything.

**Dry-run CLI.** Parses a file, indexes the real Invoice folder, matches and
prints the Chat payloads, all on an in-memory store; nothing is posted and
nothing is written to Supabase. Needs the `GOOGLE_WORKSPACE_AP_*` triple in
`.env` (or ADC with the folder shared) and ADC for Gemini
(`gcloud auth application-default login`):

```
python main.py parse C:\path\to\export.xlsx
python main.py dry-run C:\path\to\export.xlsx
python main.py dry-run C:\path\to\export.xlsx --no-ai
```

**Against the real store.** Put `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, the
`GOOGLE_WORKSPACE_AP_*` triple, `DASHBOARD_URL` and (only if you mean to
post) `CARD_WEBHOOKS_JSON` in `.env`, then:

```
python main.py index --months "2026 09,2026 10"
python main.py match --year 2026 --month 9
python main.py notify --year 2026 --month 9 --dry-run
python main.py remind
python main.py serve --port 8765
```

`serve` without `AUTH_DISABLED` is IAP mode and answers 401 to every `/api`
call, because only IAP can add the assertion header; local runs use
`AUTH_DISABLED=1` (open mode). Add `set DASHBOARD_PASSWORD=anything&&`
(bash: `DASHBOARD_PASSWORD=anything`) to try the password gate; the cookie
is not `Secure` off Cloud Run, so plain HTTP works.

**Tests** (standard library, no network): from `ap-reconciliation`,
`python -m unittest discover -s tests -v`.

## Deployment

[`.github/workflows/deploy-ap-reconciliation.yml`](../.github/workflows/deploy-ap-reconciliation.yml)
runs on every push to `main` touching `cloud_run/`, signs in to Google Cloud
through Workload Identity Federation (`docs/credentials.md`), deploys with
`--source` (one instance, 1 GiB, 30-minute request timeout, CPU always
allocated so background jobs keep running between the page's polls), mounts the
secrets above, then straight away reads both URLs and sets `DASHBOARD_URL`
(for the Chat card buttons) and `LOGIN_URL` (for *Sign out*; read from the
existing sign-in page service) on the dashboard, so a later failure cannot
leave it without them. Then it deploys the sign-in page (`login/`, public,
with the dashboard URL as `DASHBOARD_URL`) as the service
`ap-reconciliation-login`; only on that page's very first deploy does a
further step tell the dashboard its `LOGIN_URL`. The dashboard is deployed
behind Identity-Aware Proxy (`--no-allow-unauthenticated --iap`,
`IAP_AUDIENCE`; step 5 of the setup has the grants and the other two modes).
A last step deploys the Cloud Run job `ap-reconciliation-remind` from the
same image and only warns when `claude-sessions` may not manage jobs. A
push touching `login/` deploys too.

Secrets mounted as environment variables are read when an instance starts,
so a new secret version (a rotated key, an added card webhook) reaches the
service on the next instance; with `--min-instances 0` that is after the
next idle period, or at once with *Run workflow*.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Voice mode: the microphone does nothing, or says it is blocked | the browser was refused the microphone for this site: click the icon at the left of the address bar and allow it. Firefox and Safari cannot listen at all: type the question |
| Voice mode: nothing is read aloud | the browser speaks only after the page has been clicked once (browser rule), and not while the computer's sound is muted; *Say again* in the answer panel repeats the last answer |
| *Rematch* says *No invoice found yet* although the invoice is in Drive | the file is filed outside the purchase's month folder and the months either side, or Gemini could not read it (the job's read errors; files over 15 MB are skipped), or its total differs from the charge by more than the matcher allows, or another purchase already holds it. *Link...* picks it by hand |
| *Network error: Failed to fetch* in the middle of a step | the network between the browser and Cloud Run dropped a long, silent request (office proxies do this after about a minute). Each step now runs as a background job that the page polls every two seconds, so this should not recur; *Re-run matching* resumes where the read stopped |
| `409 {"status": "busy"}` from an upload, index, match or notify | one job at a time per instance; wait for it (`/health` shows `last_job`) and retry. The dashboard retries by itself |
| Invoice listed in the picker with *unsupported file type* | Word, Excel, zip or e-mail files are not read; save as PDF into the same folder. Files over 15 MB likewise. Such files are not retried; any other extraction error is retried on the next index |
| Index keeps reporting the same `remaining` count | the files that fail are listed in the job log (`errors`); fix or remove them. The dashboard stops after two stalled rounds |
| *AP token refresh failed ... invalid_grant* | the AP user's refresh token was revoked (password change, app removed under the account's third-party access, token unused for six months while the OAuth app is in *testing*). Mint a new one with the same client (OAuth Playground, scopes `https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/drive` - **both**: a Drive-only token stops the invoice filer in `mannetsang/ap-automation` with `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT` - signed in as `ap@`), add it as a new version of `google-workspace-ap-refresh-token`, redeploy |
| *Google Drive error ... 404* on the Invoice folder | the folder id is wrong, or the service is on ADC and the folder is not shared with the compute service account |
| `database error: GET transactions -> 401` (PostgREST) | `SUPABASE_SERVICE_KEY` is wrong: the anon key instead of `service_role`, or the key was rotated in Supabase. Add the right one as a new secret version and redeploy |
| `database error: ... 404 ... relation "..." does not exist` | the schema is not applied to this project: `setup_db.py --project-ref <ref>` |
| *the database is missing a table or column this needs* (502, `"schema_missing": true`), or a tab of `/admin` says it needs the database update | the code was deployed before `schema.sql` was applied (One-time setup, step 2): run `setup_db.py --project-ref bkkifrwfgtcnmfynbbdd`, then check `/admin#settings`, which lists each table and column and whether it is there. Until then the dashboard's Use case list is empty (typed text still saves), and the registry, the History and the coding lists are read-only or empty |
| `database error: ... 5xx` or connection errors for hours | the Supabase project is paused (free-tier projects pause after a week idle). Restore it from the dashboard; consider the paid tier |
| Anyone can open the dashboard without signing in | the service was deployed open (`--allow-unauthenticated --no-iap`, `AUTH_DISABLED=1`; `/api/config` says `"auth": "open"`). Deploy behind IAP as step 5 says |
| (password mode) The deploy fails, the revision's log says `DASHBOARD_PASSWORD is set but blank` | the latest version of `ap-reconciliation-dashboard-password` is empty or whitespace: add a real one as step 5 says and deploy again |
| (password mode) The page asks for a password nobody has | it is Secret Manager `ap-reconciliation-dashboard-password` (an owner reads it with `gcloud secrets versions access latest --secret=ap-reconciliation-dashboard-password --project=shp-ai-bot-2026`); set a new one as step 5 says |
| (password mode) *Too many wrong passwords* | ten wrong tries from one address, or fifty overall, within 15 minutes: wait for the window to pass (a deploy restarts the counters too) |
| (password mode) Everyone was asked for the password again at once | the password or the Supabase key got a new version, or the sessions passed 30 days; enter it again |
| (password mode) The password is right but the page keeps asking | the browser blocks cookies for the site (private mode with cookies off, a strict extension), or the service is reached over plain HTTP (the cookie is `Secure` on Cloud Run) |
| (open and password modes) *Updated by* says `anonymous` | the person left the *Your name* box in the header empty; the name is remembered per browser |
| (IAP mode) Google answers *You don't have access* | the account is not on IAP's access list: the domain grant in step 5, or a `user:` grant; a minute to take effect |
| (IAP mode) Cloud Run answers 403 *Forbidden* without first sending the browser to Google's account prompt | IAP is not enabled on the service (`--no-iap`): deploy with `--iap` |
| (IAP mode) 401 *sign in with your superhairpieces.com Google account* for a real colleague | the account got through IAP but is outside `ALLOWED_DOMAINS`; or `IAP_AUDIENCE` does not match the service (`/health?deep=1` shows it, the logs say *IAP assertion rejected*) |
| The right side of the dashboard is cut off (the last tile, *Send reminders*, the last column) | something hides part of the browser window. A touchpad pinch (it magnifies the page without laying it out again; Chrome keeps it across reloads) is handled: while pinched in, the page lays itself out for the visible area (`visualViewport`), and toasts, dialogs (a tall one scrolls inside the visible area), the top bar, the sidebar and the table header follow that area (they can trail a fast scroll or pan by a frame). Phones and tablets keep the browser's own pinch-and-pan. A window that runs past the edge of the screen gets a notice (maximize it: double-click its title bar, or Windows key + Up); hovering it shows the sizes the browser reports. A window covering the browser's right side cannot be detected |
| (IAP mode) After *Sign out*, opening the dashboard URL signs the same person straight back in | expected: *Sign out* ends the dashboard session (IAP's cookie), not the Google session, and IAP has no other signed-out state. The sign-in page's *Sign in with Google* and the header's *Switch account* make Google ask which account to use; on a shared computer the sign-in page links Google's own sign-out |
| (IAP mode) The page shows *Your Google session has ended* | IAP's session expired (about an hour); *Reload* signs in again |
| Notify says *no webhook configured for this card* | add the card's `last4` (or a `default`) to `CARD_WEBHOOKS_JSON`, new secret version, redeploy |
| Notify says *chat webhook returned 404/403* | the webhook was deleted in the space or the space was archived; create a new one and update the secret |
| Deploy fails with *Permission denied on secret* or *secret not found* | step 6 (grant) or steps 3-4 (create) were skipped for that secret |
| Deploy fails on `--iap` (*IAP API has not been used*, service agent missing) | the first two IAP commands in step 5 were not run |
| The weekly reminder never runs, or the scheduler run fails with 403 | the scheduler job was not created, or the compute account may not start the Cloud Run job: step 8 |
| *Add receipt...* is greyed out, or the receipts list says only the owner or AP can add receipts | the signed-in person is not the card's owner: AP enters their address as *Owner email* under *Cards...* |
| *The receipt was not kept* (409) after *Save receipt* | someone linked, confirmed or waived the purchase while the file was saving; the file went to the Drive trash. Reload and check the row |
| *This photo could not be prepared in this browser* | usually an iPhone HEIC photo on a Windows computer: add it on its own (it is sent as is), or take the photo on the phone |
| The Chat card's button still says *Invoice folder* for card 4421 | the card was posted before receipt upload; *Send reminders* posts a new one |
| A step says *the server restarted while this step was running* | the server was replaced mid-job (a deploy, or Cloud Run recycling the instance) and the job went with it. Uploading, reading invoices and matching are started again by the page on their own, up to three times, since each picks up where it stopped (already-read invoices are not read again, lines already stored are not stored twice); sending reminders is not, so a Chat card is never posted twice. If it still fails, *Re-run matching* in each month the statement touched |
| Upload reports `new: 0` | the same lines are already stored (ids are deterministic); nothing is lost. Legacy `.doc` is refused: save as `.docx` or `.txt`. An `.xls` that is really a web page is refused too: save it as `.xlsx` |
| Everything stays `missing` although invoices are filed | the invoices are not indexed yet (`AI_DISABLED`, or `remaining` > 0), are filed under a month folder outside month-1..month+1, or have no readable total (`extraction_error` in the picker) |
| A Chat card's *Open dashboard* button opens the wrong host, or the header has no *Sign out* | `DASHBOARD_URL` or `LOGIN_URL` is not set on the dashboard: the workflow's *Point DASHBOARD_URL and LOGIN_URL at the services* step, which runs straight after the dashboard deploy, failed; or, on the sign-in page's very first deploy, one of the two steps after it failed (if *Deploy the sign-in page* itself failed there is no page yet, and a hidden *Sign out* is expected). Re-run the workflow, or set both by hand: `gcloud run services update ap-reconciliation --region us-central1 --project shp-ai-bot-2026 --update-env-vars DASHBOARD_URL=<dashboard url>,LOGIN_URL=<sign-in page url>` |
