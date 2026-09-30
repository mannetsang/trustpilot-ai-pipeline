# Clover appointments → TeamDesk

**Webhook (primary):** a Gmail filter forwards Clover's "An appointment was
confirmed" emails to Postmark (`84f6a6edb4d8f3dfb1d9e443031b7c15@inbound.postmarkapp.com`),
which POSTs each one to the public `clover-inbound` service's `/inbound`
within seconds. Postmark authenticates with basic auth; the credential is the
`POSTMARK_INBOUND_AUTH` secret (`user:password`), and the webhook URL set in
Postmark is `https://<user>:<password>@<clover-inbound URL>/inbound`.
Postmark retries while `/inbound` returns an error.

**Poll (catch-up):** the private `clover-appointments` service described below
re-reads the last two days of mail on a timer. The POS ID check means a
booking both paths see is created once.

Every Clover booking confirmation becomes an Appointment record in
TeamDesk (database 56554, table `t_504863`).

Clover's public API has no appointment endpoint or webhook, so the email is the
only place the appointment date and time appear. A Cloud Scheduler job calls
`/poll` every 5 minutes. For each booking email in the last two days the service
reads the salon, date and time (the salon's copy, "An appointment was
confirmed", carries the same details as the customer's email), opens the receipt link for the service items,
price, customer and order ID, and creates the record unless one with that
**POS ID** (the Clover order ID) already exists.

## Field mapping

| TeamDesk | Value |
|---|---|
| Service Office | `USA` |
| Service Menu | receipt line items, e.g. `Men's Coloring` |
| Service Price | receipt order total |
| Appointment Time | email date/time with its zone |
| Appointment Status / Manager Approve | `Open` |
| Incentive Record | `Not yet` |
| Model and Color | `N/A` |
| Source | `CLOVER` |
| POS ID | Clover order ID |
| Location ID | Clover merchant ID |
| Receipt Link | the email's receipt link |
| Client Email | from the receipt |
| Internal Notes | salon, Clover employee who booked |

Hairstylist and client name are left blank for staff to fill in (Clover's email and receipt don't name the stylist).

## Credentials

All from Secret Manager, mounted by the deploy: `EMAIL_USER`,
`GOOGLE_APP_PASSWORD` (IMAP to the mailbox receiving Clover's emails) and
`TEAMDESK_TOKEN`. The service is private: Cloud Run rejects any request
without a Google identity token for an account holding `run.invoker`, which
the scheduler job sends via OIDC.

## Setup

Live since 30 Sep 2026: service `clover-appointments` (us-central1), private,
deployed on every push to main that touches `src/cloud_run/`.

The scheduler job needs Cloud Scheduler rights, which claude-sessions (the
account behind the workflows) does not have. Create it once as a project owner:

```
gcloud scheduler jobs create http clover-appointments-poll --location us-central1 --project shp-ai-bot-2026 --schedule "*/5 * * * *" --time-zone "America/Toronto" --uri "https://clover-appointments-onvg62bzra-uc.a.run.app/poll" --http-method GET --attempt-deadline 180s --oidc-service-account-email "304363458561-compute@developer.gserviceaccount.com" --oidc-token-audience "https://clover-appointments-onvg62bzra-uc.a.run.app"
```

Or grant claude-sessions `roles/cloudscheduler.admin` and run **Setup Clover
appointments sync** from the Actions tab.

Until then, `.github/workflows/clover-appointments-poll.yml` calls `/poll` every
5 minutes from GitHub Actions (GitHub may delay it several minutes). Delete that
file once the Scheduler job exists.

Dry run (writes nothing), as any account with `run.invoker` on the service:

```
gcloud run services proxy clover-appointments --region us-central1 --project shp-ai-bot-2026
```

then open `http://localhost:8080/poll?dry_run=1&days=7`.

`&days=N` backfills further back; existing POS IDs are skipped.
