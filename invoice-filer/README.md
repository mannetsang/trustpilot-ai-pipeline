# Invoice filer

Saves invoice PDFs from **ap@superhairpieces.com**'s Gmail into its Drive:

```
My Drive / Invoice / 2026 09 / GK Logistics / NEW FRONTIER 20360 21703.pdf
```

`file_invoices.py` runs hourly in GitHub Actions (`.github/workflows/invoice-filer.yml`)
and can be run by hand (`python invoice-filer/file_invoices.py --dry-run`).

## How a PDF is handled

1. Gmail search: messages since `--since` (default 2026-01-01) with a PDF
   attachment and neither filer label.
2. A PDF whose MD5 already exists under `Invoice/` is a duplicate (the same
   invoice forwarded again) and is not saved twice.
3. Gemini 2.5 Flash on Vertex AI reads the PDF and email and returns:
   is it an invoice/bill/receipt/statement, the **issuing** vendor (not the
   colleague who forwarded it), the printed invoice date and number. It is
   given the existing vendor folder names so spelling stays consistent;
   legal suffixes (Inc, Ltd, PBC...) are stripped.
4. Month folder = printed invoice date; the email's received date (Toronto)
   when no date is printed. Month and vendor folders are created on demand.
5. The file keeps its attachment name (" (2)" added on a clash) and carries
   `appProperties` `source=invoice-filer`, `gmailMessageId`, `invoiceNumber`.

## Gmail labels

| Label | Meaning |
|---|---|
| `Invoice Filer/Filed` | at least one PDF saved (or already saved) |
| `Invoice Filer/Not invoice` | every PDF judged not an invoice |

Errors leave the message unlabelled, so it is retried next run. To have a
skipped message reconsidered, remove its label. To re-file one, delete the
file in Drive and remove the label.

Auth: see `lib/google_workspace.py` (ap@ OAuth refresh token) and
`docs/credentials.md`.
