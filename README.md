# ARMCO Customer Branch Backfill

A Python command-line tool for filling **Customer Branch** on existing Zoho Books documents from a reviewed CSV mapping. Each successful update is checked against a saved original document before it is marked verified.

Approved scope: **14,450 invoices + 2,452 credit notes = 16,902 documents**. Invoice processing has passed live testing and is partially complete. Credit-note processing remains blocked.

## Current status

Snapshot from the local invoice journal on **16 November 2026, 1:03 PM Nairobi time**. These are saved counts, not a live counter.

| Invoice outcome | Count |
| --- | ---: |
| Updated and verified | 3,347 |
| Already had the expected branch | 1 |
| Halted invoice requiring investigation | 1 |
| Pending | 11,101 |
| **Total invoices** | **14,450** |

**The invoice run is halted.** Zoho returned HTTP 429, code 45, reporting exhaustion of the organisation's **15,000-call allowance**. A subsequent OAuth refresh attempt also failed. Both API access and authentication/connectivity must be available before work continues.

The interrupted invoice is **156416**. Do not assume its update succeeded or failed until its evidence and a fresh read are reconciled.

All **250 sampled credit notes** lacked invoice associations. The tested credit note, **CN027182**, could not be saved because Zoho required an Associated Invoice Number. This sample does not establish the condition of every credit note. No successful credit-note backfill has been verified.

## Operator quick start

Run commands from this repository in **PowerShell**. Copy commands from `powershell` blocks. API responses and JSON payloads are data, not shell commands.

### Check saved progress without API calls

```powershell
Get-Content .\output\corrected_run\invoice_field_id_run\summary.json
```

View the last automatic progress snapshot:

```powershell
Get-Content .\output\corrected_run\invoice_field_id_run\monitor\progress.md
```

Check its timestamp. The monitor stops on a failure or halt; an old report does not prove the updater is still running.

### Resume only after the halt is resolved

Once API access is restored and the invoice journal has **no unresolved halt**:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --run --requests-per-minute 90
```

This is the **current invoice runner**. It skips verified, already-correct and conflicting rows. It does not process credit notes or automatically clear a saved halt. Keep one updater active for this map, and keep the configured terminal, network connection and computer awake during processing.

To bound an invocation to 100 unfinished rows:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --run --limit 100
```

### Continue while invoices 157368, 160059, and 161528 remain unresolved

Invoice 157368 has a saved HTTP 401 failure and an unresolved branch field. Invoice 160059 has a successful branch write, but later reads showed customer-balance and stock changes beyond the narrow reconciliation that was approved. Invoice 161528 received an HTTP 401/code 57 rejection and its branch remains blank. These invoices must not be retried or counted as complete. Each isolation option validates the exact saved evidence before excluding that invoice; the options also clear only the matching saved halt. The 161528 isolation additionally requires both prior 157368 and 160059 isolation markers. Pending-only runs process `pending` rows only, with no overall verified-invoice target. Each invocation remains bounded by `--limit` (default: 100). To isolate 161528 and continue:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --isolate-161528-and-continue --limit 7888 --requests-per-minute 90
```

For subsequent bounded batches, use:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --continue-pending --limit 7888 --requests-per-minute 90
```

Any other failed invoice or different active halt stops continuation. Do not use `--run` while an isolated failure remains. None of the three invoices is counted as complete; resolve and reconcile them separately before declaring the invoice backfill complete.

If a later invoice hits the daily quota halt, wait until the allowance is restored and reconcile that invoice with `--reconcile-429`; then resume with `--continue-pending`.

The default request cap is 90/minute. Other integrations share the organisation's allowance. Parallel copies cannot increase that allowance.

### Automatic five-minute reports

Run this in a second terminal after the updater resumes:

```powershell
.\.venv\Scripts\python.exe watch_progress.py --output .\output\corrected_run\invoice_field_id_run --live --interval 300
```

The updater terminal prints a progress line at startup, every 10 attempted invoices, and at batch completion. It includes the batch count and overall verified count/percentage. Open [the invoice progress report](output/corrected_run/invoice_field_id_run/monitor/progress.md) in VS Code for five-minute snapshots of verified updates, already-correct invoices, remaining rows, failures, conflicts and new verified updates since the previous check. The first monitor check establishes a baseline.

To keep the five-minute updates running while an isolated failure/halt remains, add `--keep-running-on-halt`:

```powershell
.\.venv\Scripts\python.exe watch_progress.py --output .\output\corrected_run\invoice_field_id_run --live --interval 300 --keep-running-on-halt
```

The monitor reads local SQLite data, needs no credentials and makes no Zoho requests. It has a separate lock and stops after completion. Without `--keep-running-on-halt`, it stops on a failure/halt; with the flag, it continues reporting the halt status every interval. Reports update locally, not automatically in chat.

For a single snapshot:

```powershell
.\.venv\Scripts\python.exe watch_progress.py --output .\output\corrected_run\invoice_field_id_run --live --once
```

## How invoice updates work

1. Read the invoice and match its ID and number to the approved mapping.
2. Read its custom-fields endpoint to establish the branch value. An omitted lookup in the invoice response alone is not proof of emptiness. Populated or conflicting representations block a new assignment.
3. Commit the original document, request evidence and attempted state to SQLite.
4. Send only the Customer Branch field ID and mapped branch record ID using `PUT /invoices/{invoice_id}`. Do not resubmit customer, lines or totals.
5. Read back the invoice. Verify the expected branch and unchanged protected data before marking it complete. If the target field is omitted from both the pre-write and post-write invoice responses, the dedicated custom-fields endpoint may verify the branch; any populated/conflicting representation or protected-data difference blocks completion.

Comparison covers returned customer identity, lines, amounts, payments, status, other custom fields and fiscal details. The invoice runner excludes target branch representations, known modification audit fields and the previously investigated generated `CInvoiceID` token in a recognised payment URL. Other URL changes remain protected.

Checks cover data returned by Zoho, not information absent from the response. Reads and writes are not an atomic transaction, so avoid concurrent edits and perform UI spot checks. Failed or uncertain writes are not blindly resent. Business-data differences require investigation.

## Setup and credentials

Use Python **3.9 or later**. For a new environment:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Configure the following environment variables in the same PowerShell session used to launch the updater, using your local credential-management method:

| Variable | Purpose / current non-secret configuration |
| --- | --- |
| `ZOHO_CLIENT_ID` | OAuth client ID |
| `ZOHO_CLIENT_SECRET` | OAuth client secret |
| `ZOHO_REFRESH_TOKEN` | Refresh token for the authorised scopes |
| `ZOHO_ORG_ID` | `941141787` |
| `ZOHO_ACCOUNTS` | `https://accounts.zoho.com` |
| `ZOHO_API` | `https://www.zohoapis.com/books/v3` |
| `CF_INVOICE_ID` | `1462401000020819910` |
| `CF_CREDITNOTE_ID` | `1462401000020819915` |

Both branch fields use API name `cf_customer_branch`, related to `cm_customer_branch`. Their field IDs differ.

The authorised scope set used during this work is:

```text
ZohoBooks.invoices.READ
ZohoBooks.invoices.UPDATE
ZohoBooks.creditnotes.READ
ZohoBooks.creditnotes.UPDATE
ZohoBooks.creditnotes.CREATE
ZohoBooks.settings.READ
```

Invoice processing uses invoice READ/UPDATE and settings READ. Credit-note CREATE was added because Zoho documents it for the dedicated custom-fields update endpoint; the scripts do not create credit notes.

There is no automatic `.env` loader. Settings in one terminal are not automatically available in another existing session or an assistant's tool process. A `Missing settings` error does not mean progress was lost. Keep grant codes, tokens and client secrets out of chat, screenshots, source files and command history. New tokens do not bypass organisation quotas.

## Approved mapping

Active input:

```text
data/Armco_Branch_Backfill_API_Map_corrected.csv
```

Required columns:

```csv
type,zoho_id,number,branch_name,branch_record_id
```

IDs remain digit strings. `branch_record_id` identifies the branch custom-module record, not the customer. The original source CSV is retained separately; damaged IDs were reconciled by document number, and the corrected branch mapping was reviewed against a branch export.

Validate the corrected map locally, without credentials or API calls:

```powershell
.\.venv\Scripts\python.exe backfill_customer_branch.py --validate --csv .\data\Armco_Branch_Backfill_API_Map_corrected.csv
```

Validation checks required values, duplicate/conflicting keys, numeric IDs and fixed scope counts, and prints a SHA-256 fingerprint. Validation alone does not approve a mapping.

The exact map bytes, organisation, endpoints and field ID are bound to the journal. Do not edit the CSV, replace the database or switch organisations to resume an existing run. Avoid spreadsheet conversions that round long IDs.

## Files and evidence

| Path | Purpose |
| --- | --- |
| `run_invoice_backfill.py` | Current invoice-only updater and controlled recovery |
| `watch_progress.py` | Local progress snapshots and five-minute monitoring |
| `backfill_customer_branch.py` | Shared client, validation, storage and original workflow |
| `reconcile_map.py` | Read-only document and branch mapping investigation |
| `find_creditnote_candidate.py` | Bounded read-only credit-note association scan |
| `inspect_creditnote_fields.py` | Read-only field metadata for the investigated credit note |
| `inspect_cash_customer.py` | Read-only Cash Customer field diagnostics |
| `test_creditnote_branch.py` | Guarded historical CN027182 tests; existing attempts block resend |
| `repair_test_invoice.py` | Targeted historical repair of invoice 152522's description |
| `tests/` | Automated tests with simulated clients and temporary databases |
| `data/` | Local mappings and exports, ignored by Git |
| `output/corrected_run/progress.sqlite3` | Original investigation journal; its halt is preserved |
| `output/corrected_run/invoice_field_id_run/progress.sqlite3` | Authoritative invoice-only journal |
| `output/corrected_run/invoice_field_id_run/report.csv` | Invoice mapping, status and error details |
| `output/corrected_run/invoice_field_id_run/summary.json` | Invoice status totals |
| `output/corrected_run/invoice_field_id_run/monitor/` | Progress snapshots, history and separate monitor reports |

The current runner depends on the original investigation database and its invoice 152522 snapshot. When creating a new invoice journal, startup verifies that this invoice's targeted repair still matches its original business data and expected branch. Once the separate invoice journal exists, `--continue-pending` and `--reconcile-stock-only` use that journal's bound map, organisation and field identity instead of rereading the unrelated 152522 invoice. This is a continuation of the reviewed project, not a generic updater that can start with only a CSV.

SQLite holds original documents and timestamped `evidence:*` records for requests, outcomes, readbacks and differences. The journal writer waits up to 30 seconds for transient database contention; still run only one invoice updater at a time. If a lock persists, retain the exact error and do not delete the database, lock file or evidence. API rejection evidence may also appear in `access_error_*.json`. These files can contain customer data. Retain the whole output directory, restrict access, and back it up before journal maintenance. Never delete evidence or halt markers to force a retry.

`.gitignore` excludes credentials, local data, output, databases, logs and the virtual environment. Review staged files before committing; ignoring does not untrack files already committed. Repository visibility has not been verified. Do not publish customer data or credentials.

## Troubleshooting and recovery

| Result | Meaning and next action |
| --- | --- |
| `Missing settings` | Configure variables in this terminal. Do not recreate the journal. |
| `OAuth refresh failed after bounded retries` | Refresh could not complete; investigate connectivity or Zoho availability. This alone does not prove the token invalid. |
| HTTP 429, code 45, allowance message | Daily quota exhausted. Stop retries; confirm reset time or increased allowance with Zoho. |
| HTTP 429, code 44 | Per-minute restriction; coordinate other integrations and reduce the request cap. |
| `Invoice-only run halted` | Inspect saved request, response and readback before recovery. Restarting does not clear it. |
| `Recovery is restricted...` | The halt does not match the narrow recovery procedure, or recovery already completed. Inspect saved state. |
| `429 recovery ...` | The single-invoice recovery guards did not pass. Keep the halt and inspect saved evidence; do not edit SQLite. |
| Protected-data differences | Keep the halt and investigate exact differences. Do not relax comparisons to continue. |
| Credit-note code 12069 | Associated Invoice Number required for the tested update. Do not invent a link. |
| Credit-note code 120121 | Dedicated endpoint rejected the field index. Do not substitute another field's index. |

For the **specific old halt** `Read failed after bounded retries`, this controlled command exists:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --reconcile-read-failure
```

It requires exactly one saved attempted invoice, its expected request and a successful write response. It reads without rewriting the invoice. Verified branch and unchanged business data allow it to back up the invoice journal, mark the row verified and atomically clear that specific halt.

For the specific uncertain HTTP 429 attempt on invoice **156416**, after Zoho access and allowance are restored, use this guarded recovery:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --reconcile-429 --requests-per-minute 90
```

It requires the exact saved 429 halt, one actionable failed invoice, the original branch-only request and its uncertain-write evidence. When `--continue-pending` is active, it excludes only invoices with exact saved isolation markers (157368 and, if explicitly approved, 160059). It takes a fresh invoice read and custom-fields read, checks the approved branch and compares protected business data with the saved original. If the expected branch is already present, it records the invoice as verified without another write. If the branch is still blank and business data is unchanged, it backs up the journal and makes one branch-only retry followed by verification. Conflicts, differences, ambiguous evidence, or API failures retain the halt. Do not run this during quota exhaustion or edit the journal manually.

After this recovery succeeds and clears the halt, resume with `--run`, unless a pending-continuation marker exists; in that case, resume with `--continue-pending`.

For the specific uncertain write on invoice **157368** where the normal invoice response omits Customer Branch, use:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --reconcile-omitted-field --requests-per-minute 90
```

This command is restricted to that invoice and exact saved attempt. It compares a fresh invoice response with the saved business-data snapshot and checks the dedicated custom-fields endpoint. If the mapped branch is already present, it records verification without another write. If the branch is still blank and all checks remain consistent, it backs up the journal, sends one branch-only retry, then verifies unchanged invoice data and the branch. When the full invoice response still omits the target field, the dedicated endpoint supplies the branch verification. Conflicts, differences, ambiguous evidence, or API failures retain the halt. After it succeeds, resume with `--run`.

If that exact retry is rejected with HTTP 404/code 1002 (`Inactive template cannot be used`) and an administrator confirms HQ Template is active in the approved organisation, one further attempt is available:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --retry-inactive-template --confirm-template-active --requests-per-minute 90
```

This mode requires the saved 404 response, rechecks the template-bound invoice snapshot and blank branch, and allows only one more branch-only update. If it is rejected or cannot be verified, the halt remains.

If that retry is also rejected with code 1002, Zoho documents a dedicated [Update custom field in existing invoices](https://www.zoho.com/books/api/v3/invoices/#update-custom-field-in-existing-invoices) endpoint at `PUT /invoice/{invoice_id}/customfields`. This restricted final recovery command uses that endpoint once for invoice 157368:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --retry-dedicated-customfield --requests-per-minute 90
```

It verifies the exact saved 404 history, rechecks the invoice snapshot and current branch, backs up the journal, and sends only the Customer Branch field. It clears the halt only after readback and branch verification. If it fails, do not retry again or resume the batch; contact Zoho Support.

If the dedicated write is rejected with HTTP 401/code 57, a user with invoice edit access can set the Customer Branch field on invoice 157368 in Zoho Books to the mapped branch. Then run this read-only reconciliation:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --reconcile-manual-branch
```

It compares the invoice's protected data with the saved snapshot and verifies the mapped branch through the custom-fields endpoint. It does not write to Zoho and clears the halt only if both checks pass. If it succeeds, resume the remaining invoices with `--run`; if it fails, keep the halt and investigate.

For the specific successful write on invoice **160059**, a guarded read-only reconciliation was attempted:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --reconcile-stock-only
```

The fresh reads found changes outside the five recorded stock-availability fields, including customer balance and stock values on multiple line items. The reconciliation therefore stops and keeps the halt; do not broaden its allowed-differences list. If the owner explicitly approves isolating 160059 instead, use `--isolate-160059-and-continue` above. This never marks 160059 verified and never writes to that invoice.

After Ctrl+C or a crash with no saved halt, normal startup reads `updating` rows. Verified saved results are not rewritten. An uncertain request with a blank result remains blocked. If interrupted before any request was journalled, the original is retained as evidence before the row is reset.

## Credit notes: unresolved work

Observed results on CN027182:

| Update route / format | Result |
| --- | --- |
| Dedicated custom-fields POST with document index 1 | Code 120121: invalid custom-field index |
| Credit-note PUT with stable `customfield_id` | Code 12069: specify Associated Invoice Number |
| Browser edit/save | Same missing-invoice-association error |

Settings metadata reported index 2; document and custom-fields GET responses reported index 1. The latter assigned index 2 to Fiscalization Status. These indexes are not interchangeable.

Ask Zoho for a supported branch-only update without adding an invoice association. If associations must be repaired, establish correct links from original source records first. Reference text or customer-name similarity alone is insufficient evidence. More API allowance does not fix this validation blocker.

`find_creditnote_candidate.py` supports bounded read-only searches using `--limit` and `--skip`. It looks for a mapped, non-void credit note with an existing invoice ID/number and blank branch. It consumes API calls but makes no updates. The first 250 non-test records were already checked; do not repeat those scans during quota exhaustion.

Historical CN027182 attempts are preserved and guarded against resend. Do not delete their markers, use them as a general credit-note backfill, or resubmit the full browser document payload.

## API allowance and timing

The current invoice method needs approximately **four calls per changed invoice**: invoice read, custom-field read, update and verification read. Startup, diagnostics, recovery and retries add overhead.

At 90 calls/minute, the ideal upper bound is approximately **112 changed invoices per five minutes**, before network delays. A target of 500 would require about 400 calls/minute with this method. More daily allowance does not automatically increase the per-minute limit.

The last observed organisation allowance was **15,000 calls**. Remaining invoice work needs roughly **44,400 calls**, plus overhead. A provisional budget for all remaining work is approximately **60,000 usable calls**, including estimated credit-note processing and a buffer. The credit-note estimate depends on resolving its update method and may change.

Confirm purchased versus included allowance, validity period, reset timezone, price and other integrations' consumption with Zoho before buying. There is no confirmed finish time while quota waits and credit-note validation remain unresolved.

- [Zoho Books API limits](https://www.zoho.com/books/api/v3/introduction/#api-call-limit)
- [Increasing the daily API allowance](https://www.zoho.com/us/books/kb/automation/increase-daily-limit-api.html)
- [Invoice API documentation](https://www.zoho.com/books/api/v3/invoices/)
- [Credit-note API documentation](https://www.zoho.com/books/api/v3/credit-notes/)

For support, provide organisation **941141787**, HTTP status, code, message and requested allowance. Ask for the exact reset time/timezone. Never include tokens, cookies, secrets or authorisation headers.

## Verification and handoff

List a sample of verified invoices locally:

```powershell
Import-Csv .\output\corrected_run\invoice_field_id_run\report.csv |
    Where-Object { $_.status -eq "updated_verified" } |
    Select-Object -First 10 number, branch_name
```

Search those invoice numbers in Zoho and inspect Customer Branch, customer, lines, totals, payments and fiscal fields without saving edits. Retain reviewer, date and findings locally. Monitor CSV/summary files are separate snapshots; use the invoice journal and final root-level reports for reconciliation.

| Status | Meaning |
| --- | --- |
| `updated_verified` | Changed branch passed API verification |
| `already_correct` | Expected branch found; no update needed |
| `conflict` | Different existing branch left unchanged |
| `failed` | Investigation required; may include an unverified write |
| `pending` | Unfinished; exported totals also include updating rows |

Never count failed, pending or merely reviewed rows as complete.

Before declaring the project complete:

- Reconcile invoice totals; resolve or explicitly report failures/conflicts.
- Resolve the credit-note update method and reconcile all 2,452 credit notes.
- Finish UI spot checks across relevant customers and retain evidence.
- Back up original snapshots and reports; provide Collins the final results.
- Keep customer merges and unrelated accounting changes outside this backfill.

## Development checks

Run the suite without accessing Zoho:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Tests cover mapping validation, API pacing/retries, lookup handling, durable evidence, branch-only payloads, protected-data checks, candidate selection and recovery. Simulated tests do not establish live endpoint behaviour; live evidence is retained separately.

The original `backfill_customer_branch.py` workflow remains available for validation, diagnostics and historical investigation. Its combined invoice/credit-note run has separate test/approval gates and remains halted in the original journal. It is not the current invoice processing command. Do not run a second live workflow alongside `run_invoice_backfill.py`.
