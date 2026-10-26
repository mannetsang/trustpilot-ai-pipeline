# AP credit-card reconciliation

Accounts payable (`ap@superhairpieces.com`) uploads a credit-card statement or
an RBC **Transaction Export** (one `.xlsx`/`.csv` with every card of the
account). The service in [`cloud_run/`](cloud_run/) stores each line once,
reads every invoice filed in the AP Invoice folder on Drive with Gemini,
pairs purchases with invoices, posts an *invoices needed* card to each credit
card's own Google Chat space, and serves a dashboard, organised by year and
month, where card owners record the cost centre and usage of each charge and
link or waive invoices.

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

1. **Upload** (`POST /api/statements`). `.xlsx`, `.xlsm`, `.csv` and `.tsv`
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
3. **Match** (`POST /api/match`). Rules first, Gemini judge for the rest;
   see the next section.
4. **Notify** (`POST /api/notify`). One Chat card per credit card that still
   has purchases without an invoice, listing the missing purchases, the
   possible matches to confirm and the number of charges without a cost
   centre, with *Open dashboard* (deep link to the card's month) and
   *Invoice folder* buttons. Every post is logged in `notifications`.
5. **Weekly reminder** (the Cloud Run job `ap-reconciliation-remind`, which
   Cloud Scheduler starts on Mondays 14:00 UTC; it runs `python main.py remind`).
   For each card, the newest month that still has missing or possible
   purchases is re-indexed, re-matched and notified again.

`POST /api/statements?sync=1` runs steps 1-4 in one request (the CLI, curl
and the tests use it); the dashboard's *Upload and process* button runs them
as separate calls so it can show progress. Jobs serialize on an in-process
lock: a second index, match, notify or synchronous upload gets `409 busy`
while one runs.

## Matching rules and statuses

Every purchase is `matched`, `possible`, `missing` or `waived`; credits,
payments and fees are `not_required`. Matching only ever touches purchases
that are `missing` or `possible`: a manual link, confirmation or waiver is
never undone by a re-run.

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
| `ap` | the addresses in `AP_EMAILS` (`ap@`, `manne@`, `snow@`, added 2026-10-20); everyone, in password and open mode | upload, index, match, notify, edit cards, edit every transaction |
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
  found, missing, possible, waived, charges without a cost centre.
- **AP panel** (AP only), one row: a slim drop zone for the statement
  (drop the file on it or click it), *Upload and process*, then
  *Re-run matching*, *Send reminders*,
  *Cards...* (label, holder, owner e-mail, Chat space name, active flag,
  and whether a webhook is configured for the card), plus the step list
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
- **Card chips** ("1610 - Yin Wei", the full label on hover; one line that
  scrolls sideways when there are more cards than fit) and a status filter
  (all, missing, possible, matched, waived, no cost centre), a search box
  over description, amounts, cost centre and usage, and on phones also a
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
  (with the original foreign amount), cost centre dropdown (the
  `cost_centers` table, with an *Other...* free text), usage, note, and the
  invoice column: the linked file with vendor, number and total, or the
  buttons *Confirm* / *Not this* on a possible match, *Link...* / *No
  invoice* on a missing one, *Undo* on a waived one. Edits save on change.
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
  with receipt upload (`RECEIPT_UPLOAD_CARDS`, today 4421). The dialog takes
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
  buttons, then cost centre and usage; a coloured edge (red missing, amber
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

On cards with receipt upload (`RECEIPT_UPLOAD_CARDS`, today 4421) the card's
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
| `GET /health` | none | `{"ok": true, "store": ..., "last_job": ...}`; `?deep=1` adds reachability checks for Supabase and the Invoice folder, how many card webhooks are configured (counts only: it answers without the password) and the auth mode. (`/healthz` is answered by Cloud Run's front end itself, not the container.) |
| `GET /api/config` | none | `auth` (`password`, `open` or `iap`), `auth_disabled`, dashboard URL, sign-in page URL (`login_url`), Invoice folder URL, `ai_enabled` |
| `POST /api/login` | none, but `X-Requested-With: XMLHttpRequest` in password mode (403 without) | password mode: `{"password": "..."}` sets the session cookie (401 wrong, 429 after too many wrong ones); a no-op in the other modes |
| `POST /api/logout` | none, but `X-Requested-With: XMLHttpRequest` in every mode (403 without) | clears the session cookie (*Lock*) |
| `GET /api/me` | signed in | e-mail, name, role, cards owned |
| `GET /api/months` | signed in | month navigation with per-card counts, cards, cost centres |
| `GET /api/transactions?year&month[&card]` | signed in | the month's transactions with their invoice, `editable` flag, uploads and recent notifications |
| `PATCH /api/transactions/<id>` | signed in, AP or the card's owner | body: `cost_center`, `usage`, `note`, and/or `action` (`waive` with `reason`, `unwaive`, `confirm`, `link` with `invoice_id`, `unlink`) |
| `POST /api/ask?year&month[&card]` | signed in | voice mode: `{"question": "..."}` (up to 500 characters; `card` is four digits) answered as `{"answer", "source": "gemini" \| "summary"}` for the browser to read aloud; read only |
| `POST /api/transactions/<id>/rematch` | signed in, AP or the card's owner | *Rematch*: reads new invoices for the purchase's months, then looks for its invoice again, as a background job (202 with the job, polled through `/api/jobs/current?id=`; 409 while another job runs; 400 for a waived, non-purchase or hand-linked row). The job's result has `outcome` (`found`, `same`, `kept`, `none` or `conflict`), the status and invoice the row now has, `changed`, `invoices_read`, `read_errors` with `read_error_files`, and the updated row; its progress has `stage` and `percent` |
| `GET /api/transactions/<id>` | signed in | one row as the table shows it (with `checks` and `invoice.reading`); the receipt dialog polls it while the file is read |
| `GET /api/transactions/<id>/receipt-target` | signed in, AP or the card's owner, card with receipt upload | where *Save receipt* would put the file: `month_folder`, `month_exists`, `vendor_folder`, `vendor_exists`, `source` (`this_month`, `earlier_month` or `new`), the month's `folders`, `limits`. Creates nothing |
| `POST /api/transactions/<id>/receipt` | signed in, AP or the card's owner, card with receipt upload | multipart: one to ten `file` parts (photos; or one PDF), optional `vendor_folder` and `upload_id`. Saves to Drive and links the purchase: `201 {"status": "saved", "file", "transaction", "reading"}`; the same `upload_id` again answers `200` with `"replayed": true`, also when it arrives while the first is still saving (its own copy is trashed); `409 not_saved` when the purchase already has its invoice, is waived or changed meanwhile (the file is then trashed); 413 too large (a body over 16 MB is refused before it is read), 415 not a photo or PDF, 429 more than 40 in ten minutes |
| `DELETE /api/transactions/<id>/receipt` | signed in, AP or the card's owner | *Remove...*: unlinks a receipt added from the dashboard and sends its file to the Drive trash; the file stays out of matching even when the trash fails (`200` with `"trashed": false` and a `warning`) |
| `GET /receipts/<last4>/<YYYY>/<MM>` | signed in | the Chat card's *Add receipts*: redirects to the receipts list |
| `GET /api/invoices?year&month` | signed in | invoices of month-1..month+1 with a `linked` flag (the picker) |
| `GET /api/cards` | signed in | cards |
| `POST /api/cards` | AP | `{"last4", "label", "holder_name", "owner_email", "chat_space", "active"}`; `owner_email` may list several addresses (commas, semicolons or spaces), stored lower-case as `a@x, b@x`; 400 if one is not an address |
| `GET /api/statements?year&month` | signed in | uploads that touched the month |
| `POST /api/statements[?background=1]` | AP | multipart `file`; `?sync=1` also indexes, matches and notifies (`&notify=0` to skip the cards) |
| `POST /api/index?year&month[&limit]` | AP | read up to `limit` (max 100) new invoices for month-1..month+1 |
| `POST /api/match?year&month[&card]` | AP | pair purchases with invoices |
| `POST /api/notify?year&month[&card][&dry_run=1]` | AP | post the Chat cards (`dry_run` returns the payloads instead) |
| `POST /api/jobs?kind=index\|match\|notify&year&month[&card][&limit=8]` | AP | the same step as a background job, answered at once with `202 {status: started, job}` (`409 busy` while another runs). What the dashboard uses: an invoice read or a match can run for minutes and office networks drop a silent response after a minute or so. `index` loops over batches of `limit` files until nothing new is left |
| `GET /api/jobs/current[?id=…]` | signed in | the job with that id (the last ten stay readable, so a tab keeps its own result when someone else starts the next job; 404 after a restart), else the newest: `id`, `name`, `started`, `finished`, `running`, `progress` (`read`, `live`, `remaining`, `note`, `year`, `month`, …) and, once finished, `result` or `error` |

Job endpoints answer `409 {"status": "busy"}` while another job runs and
`500 {"status": "error"}` with the message when one fails.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `STORE` | `supabase` | `memory` keeps everything in-process (tests, demos) |
| `SUPABASE_URL` | | `https://bkkifrwfgtcnmfynbbdd.supabase.co`, set in the deploy workflow |
| `SUPABASE_SERVICE_KEY` | | service-role key; secret |
| `INVOICE_FOLDER_ID` | the Invoice folder above | root of the walk |
| `STATEMENTS_FOLDER_ID` | empty | when set, uploads are copied to Drive under `<folder>/<YYYY MM>/` |
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
| `RECEIPT_UPLOAD_CARDS` | `4421` | cards (comma-separated last four digits, or `all`) whose rows get *Add receipt...* and whose Chat card gets *Add receipts*. The default lives in `main.py`, so the service and the weekly job agree; when setting it in the workflow, set it on both |

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
| `google-workspace-ap-refresh-token` | `GOOGLE_WORKSPACE_AP_REFRESH_TOKEN` | already exists: `ap@superhairpieces.com`'s refresh token with the Drive scope |
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
2. **Schema.** Applied to `bkkifrwfgtcnmfynbbdd` on 2026-10-09 (23 statements).
   To re-apply after a schema change: the Management API token is Secret
   Manager `SUPABASE_ACCESS_TOKEN` (or a personal token from avatar, *Account*,
   *Access Tokens*, in the gitignored root `.env` as `SUPABASE_ACCESS_TOKEN=...`), then
   `python ap-reconciliation\setup_db.py --project-ref bkkifrwfgtcnmfynbbdd`
   and expect `ok: applied 23 statements from schema.sql to project bkkifrwfgtcnmfynbbdd`.
   The Table Editor now shows `cards`, `statements`, `invoices`,
   `transactions`, `notifications`, `cost_centers` and the `month_summary`
   view. Delete the token from `.env` (or keep it for future schema changes).
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
   IAP are involved; the scheduler job is created once by an owner:
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
9. **Cards.** Open the dashboard as AP, upload the first export, then under
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
is editable, and the cost centre, usage, waive and link actions all work
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
| *AP token refresh failed ... invalid_grant* | the AP user's refresh token was revoked (password change, app removed under the account's third-party access, token unused for six months while the OAuth app is in *testing*). Mint a new one with the same client (OAuth Playground, scope `https://www.googleapis.com/auth/drive`, signed in as `ap@`), add it as a new version of `google-workspace-ap-refresh-token`, redeploy |
| *Google Drive error ... 404* on the Invoice folder | the folder id is wrong, or the service is on ADC and the folder is not shared with the compute service account |
| `database error: GET transactions -> 401` (PostgREST) | `SUPABASE_SERVICE_KEY` is wrong: the anon key instead of `service_role`, or the key was rotated in Supabase. Add the right one as a new secret version and redeploy |
| `database error: ... 404 ... relation "..." does not exist` | the schema is not applied to this project: `setup_db.py --project-ref <ref>` |
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
| Upload reports `new: 0` | the same lines are already stored (ids are deterministic); nothing is lost. Legacy `.xls` and `.doc` are refused: save as `.xlsx`, or `.docx`/`.txt` |
| Everything stays `missing` although invoices are filed | the invoices are not indexed yet (`AI_DISABLED`, or `remaining` > 0), are filed under a month folder outside month-1..month+1, or have no readable total (`extraction_error` in the picker) |
| A Chat card's *Open dashboard* button opens the wrong host, or the header has no *Sign out* | `DASHBOARD_URL` or `LOGIN_URL` is not set on the dashboard: the workflow's *Point DASHBOARD_URL and LOGIN_URL at the services* step, which runs straight after the dashboard deploy, failed; or, on the sign-in page's very first deploy, one of the two steps after it failed (if *Deploy the sign-in page* itself failed there is no page yet, and a hidden *Sign out* is expected). Re-run the workflow, or set both by hand: `gcloud run services update ap-reconciliation --region us-central1 --project shp-ai-bot-2026 --update-env-vars DASHBOARD_URL=<dashboard url>,LOGIN_URL=<sign-in page url>` |
