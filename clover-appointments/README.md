# Clover appointments → TeamDesk

Every Clover "Appointment confirmed" email becomes an Appointment record in
TeamDesk (database 56554, table `t_504863`).

Clover's public API has no appointment endpoint or webhook, so the email is the
only place the appointment date and time appear. A Cloud Scheduler job calls
`/poll` every 5 minutes. For each booking email in the last two days the service
reads the salon, date and time, opens the receipt link for the service items,
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

1. Actions → **Setup Clover appointments sync** (grants the secrets).
2. Actions → **Deploy Clover appointments sync** (also runs on push to main).
3. Before anything writes, check a dry run with your own Google login
   (you need `run.invoker` on the service, which project owners have):
   `gcloud run services proxy clover-appointments --region us-central1 --project shp-ai-bot-2026`
   then open `http://localhost:8080/poll?dry_run=1&days=7`.
4. Re-run **Setup** to create the scheduler job.

`&days=N` backfills further back; existing POS IDs are skipped.
