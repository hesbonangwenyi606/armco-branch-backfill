# Armco Branch Backfill

One-time Python 3.9+ tool that fills **Customer Branch only** on **14,450 invoices
and 2,452 credit notes (16,902 total)** from an approved CSV. No frontend.

## Delivery status

The local CLI, simulated API tests and runbook are implemented. An existing
GitHub `origin` is configured; its private visibility is not yet verified.
No approved CSV or credentials were supplied. No live discovery, tests, updates,
UI verification or handoff have been performed. Simulated tests cannot confirm
this organisation's lookup payload: complete live acceptance before full use.

## Setup and files

```text
README.md                    Setup, commands, scope, recovery and handoff
.gitignore                   Excludes credentials, customer data and results
requirements.txt             requests; tests use standard-library unittest
backfill_customer_branch.py   Main Python CLI
data/                        Local approved mapping CSV (ignored)
output/                      Local SQLite, logs and reports (ignored)
tests/                       Simulated API and recovery checks
```

PowerShell, in this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python backfill_customer_branch.py --help
```

If activation is blocked, use `.\.venv\Scripts\python.exe` directly. Open this
folder in VS Code and select that interpreter. Confirm the GitHub repository is
**private** before pushing code. Commit source and tests only. `.env*`, data,
output, logs, databases and the virtual environment are ignored, apart from
`.gitkeep` placeholders. Do not save customer data or secrets in other tracked
paths. Check staged contents: ignoring does not untrack existing files.

## Approved scope and CSV validation

Put the supplied file at `data/Armco_Branch_Backfill_API_Map.csv` with headers:

```csv
type,zoho_id,number,branch_name,branch_record_id
```

Types must be `invoice` or `creditnote`. IDs remain strings, including leading
zeros; only ASCII digits are accepted. Validation rejects missing headers/values,
malformed rows, duplicate document keys (`type`, `zoho_id`), conflicting mappings
and counts that differ from the fixed approved scope. Document numbers must
also match Zoho when read.

```powershell
python backfill_customer_branch.py --validate
```

This needs no credentials and prints counts and a SHA-256 fingerprint. Counts
alone do not prove approval. Review the actual file and fingerprint, confirm
excluded November documents are absent, and keep that review locally. The CSV
is the sole inclusion list; the tool does not infer date exclusions or enumerate
other documents. Do not add rows just to meet counts. The exact file bytes,
organisation, endpoints and field IDs are bound to SQLite; changing any blocks
resume. No production count-override option is provided.

## Repairing incorrect IDs in the supplied map (read-only)

If validation reports conflicting document IDs, do not deduplicate rows or
guess replacement IDs. The supplied file can have correct document numbers and
scope but damaged IDs. `reconcile_map.py` preserves the source CSV and reads
Zoho list endpoints to resolve exact numbers within each module:

```powershell
.\.venv\Scripts\python.exe reconcile_map.py
```

Run this in the PowerShell session containing the six `ZOHO_*` settings. Only
invoice/credit-note READ scopes are needed for this first stage; no `CF_*` IDs
are needed. It scans all pages to detect ambiguous numbers but includes only
the approved source numbers in its output. Per-module source counts remain
fixed at 14,450 and 2,452; duplicate source numbers fail. No fuzzy matching,
rounding repair, new source rows or remote updates are performed.

Results are saved in a timestamped folder under `output/reconciliation/`:

- `document_matches.csv`: original and resolved IDs, exact document-match
  results, customer IDs/names, dates and branch verification results.
- `summary.json`: counts, source fingerprint, organisation, endpoint and review status.
- `candidate_map.csv`: created **only if every document and branch matches
  uniquely** and the resulting map passes normal production validation.

The documents-only command intentionally reports `branch_unverified` and creates
no candidate map. Document matching success is not branch or scope approval.
Review customer/branch associations and excluded November documents using the
evidence; a name match alone cannot establish the intended customer relationship.

To also read the Customer Branch module:

```powershell
.\.venv\Scripts\python.exe reconcile_map.py --verify-branches
```

Automatic module discovery uses `GET /settings/modules`, documented as requiring
`ZohoBooks.settings.READ`. [Custom-module record listing](https://www.zoho.com/books/api/v3/custom-modules/#list-custom-module-records)
is currently documented with `ZohoBooks.custommodules.ALL`, which is broader than
the existing token. The script never requests extra permissions automatically.
If access is rejected, it still writes the document-match report and leaves
branches unverified. Resolve access with the owner before requesting broader
scopes, or obtain an authoritative branch export for a separately reviewed
reconciliation. No custom-module write operations exist in this script.

If the exact API name is known, bypass module discovery with
`--verify-branches --branch-module cm_EXACT_API_NAME`. Do not guess that value.
Branch names are matched exactly and case-sensitively against `record_name` by
default; use `--branch-name-field cf_EXACT_FIELD` only if metadata identifies a
different field. Duplicate names or missing branches block candidate creation.

Successful API pages are durably cached in
`output/reconciliation/read_cache.sqlite3`; rerun the same command to resume
after network/quota interruptions. The cache binds the original file fingerprint,
organisation and endpoints. It is an evidence snapshot, not a continuously
refreshed view. For an intentional fresh scan keep the old evidence and choose a
new `--output output/reconciliation_fresh` directory. New reports have unique
timestamped folders so old candidates cannot appear to belong to a failed rerun.
The default maximum is 1,000 pages per listing; `--max-pages` can raise it.

Review and approve a candidate fingerprint before using it with the backfill's
`--csv` option and a separate progress directory. The original approved CSV and
existing backfill progress are never overwritten. Reconciliation logs/caches
contain customer data and remain ignored by Git. Avoid opening/saving ID files
in a spreadsheet that converts IDs to floating-point numbers.

## Zoho configuration and discovery

Create OAuth credentials for the correct data centre with these requested scopes:

```text
ZohoBooks.invoices.READ
ZohoBooks.invoices.UPDATE
ZohoBooks.creditnotes.READ
ZohoBooks.creditnotes.UPDATE
```

Set environment variables through your local secret-management mechanism:

| Variable | Meaning |
| --- | --- |
| `ZOHO_CLIENT_ID` | OAuth client ID |
| `ZOHO_CLIENT_SECRET` | OAuth client secret |
| `ZOHO_REFRESH_TOKEN` | OAuth refresh token |
| `ZOHO_ORG_ID` | Books organisation ID; independently confirm in Zoho UI |
| `ZOHO_ACCOUNTS` | Accounts base, e.g. `https://accounts.zoho.com` |
| `ZOHO_API` | Matching base, e.g. `https://www.zohoapis.com/books/v3` |
| `CF_INVOICE_ID` | Invoice Customer Branch customfield_id |
| `CF_CREDITNOTE_ID` | Credit-note Customer Branch customfield_id |

No automatic `.env` loader is included. Never store real credentials in tracked
files or command history. Discovery needs the six `ZOHO_*` settings but no field
IDs. Substitute real IDs from the correct organisation:

```powershell
python backfill_customer_branch.py --discover INVOICE_ID --type invoice
python backfill_customer_branch.py --discover CREDITNOTE_ID --type creditnote
```

Discovery prints custom fields, potentially including customer values, locally.
If discovery returns `[]`, inspect the response structure without printing values:

```powershell
python backfill_customer_branch.py --diagnose DOCUMENT_ID --type invoice
```

This read-only diagnostic prints key names, field metadata and value types/empty
states. It distinguishes absent fields from an empty custom-field list and
identifies branch-related top-level or hash entries. It does not treat omission
as an empty branch or enable updates. Use `--type creditnote` for credit notes.

To inspect the configured lookup schema independently of a document response,
add `ZohoBooks.settings.READ` to a new OAuth grant and exchange it for a new
refresh token. Existing refresh tokens do not acquire scopes automatically.
Then run these read-only commands (the corresponding `CF_*` ID must be set):

```powershell
python backfill_customer_branch.py --field-metadata --type invoice
python backfill_customer_branch.py --field-metadata --type creditnote
```

They read `GET /settings/fields` for the configured ID and entity and print schema
metadata without lookup records or default values. No remote settings change,
document update, empty-field assumption or live-test approval occurs.

Identify Customer Branch using actual metadata, then set both field IDs. Do not
substitute customer/contact IDs for branch record IDs. A configured field must
appear exactly once in the GET response. Absent fields are not assumed empty.
Lookup values must be empty or numeric record IDs; any other representation
fails closed pending a reviewed adapter and live contract check.

### API contract to confirm live

Zoho documentation checked on 15 November 2026 describes:

- [Invoice custom fields](https://www.zoho.com/books/api/v3/invoices/#update-custom-field-in-existing-invoices):
  `PUT /invoice/{id}/customfields`, singular `invoice`, with
  `{"custom_fields":[{"customfield_id":"FIELD_ID","value":"BRANCH_RECORD_ID"}]}`.
- [Credit-note custom fields](https://www.zoho.com/books/api/v3/credit-notes/#update-credit-note-custom-fields):
  `POST /creditnotes/{id}/customfields`, with
  `{"custom_fields":[{"index":2,"value":"BRANCH_RECORD_ID"}]}`.
  The tool derives index (1..46) from the field matching `CF_CREDITNOTE_ID` in
  the fresh GET response; it never guesses an index.

**The credit-note endpoint currently documents `ZohoBooks.creditnotes.CREATE`,
which differs from the requested UPDATE scope.** Confirm this discrepancy with
Zoho or in the controlled live test. If UPDATE is rejected, resolve the contract
with the owner before broadening access. The tool never creates credit notes,
requests extra scopes automatically or switches to a general document update.
Confirm both endpoints accept lookup record IDs and preserve omitted custom
fields. A successful simulated response cannot establish that behaviour.

## Dry run

### Invoice-only field-ID successor run

After investigating the original halt, `run_invoice_backfill.py` provides an
invoice-only journal under `output/corrected_run/invoice_field_id_run`. It keeps
the original run database and halt unchanged. Every invocation first verifies
that invoice 152522 still matches its original business data after the targeted
description repair, with the expected branch saved. Map and Zoho identity must
match the original run.

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --test
```

The test stops after one changed invoice is API-verified. It first reads the
invoice and its documented custom-fields GET endpoint; only an explicitly blank
lookup can be written. The request is a single-field-ID PUT to the invoice
resource, never a full document payload. Successful test verification is a
prerequisite for the following command:

```powershell
.\.venv\Scripts\python.exe run_invoice_backfill.py --run
```

The invoice runner now defaults to 90 requests/minute; explicitly select it with
`--requests-per-minute 90`. This is about 6% less pacing time than 85, not a
solution to daily quotas. Existing processes keep their old configuration until
restarted. After Ctrl+C, startup reads any interrupted invoice and verifies the
saved result without resending the request. An uncertain blank result remains
blocked for investigation. Keep only one runner active.

`--limit 100` bounds the number of documents examined in an invocation. Final
statuses are skipped on resume; any saved write attempt is never blindly
resent. A rejected write or protected-data difference halts the successor run
for investigation. Raw evidence is retained transactionally in its SQLite
journal; report.csv and summary.json update every 10 documents. Other custom
fields, amounts, line items, payments, customer identity and fiscal details must
compare equal. Only branch mirrors, modification audit fields and the
previously investigated generated CInvoiceID payment-link token are excluded.
An omitted branch after the write cannot pass verification. Credit notes are
never processed by this runner. At roughly four API calls per changed invoice
and 85 calls/minute, all 14,450 invoices require over 11 hours of request pacing,
plus network overhead; one hour cannot complete the whole backfill.

If a successful invoice write is followed by `Read failed after bounded retries`,
use `run_invoice_backfill.py --reconcile-read-failure` before resuming. This mode
requires exactly one saved attempted invoice, its exact branch-only request and
one successful write response. It performs a fresh read without rewriting the
invoice. Only matching business data and the expected branch allow it to back up
the successor database, mark that invoice verified and atomically clear this
specific successor halt. It cannot clear other halt reasons. The original bulk
database remains unchanged. On success, resume using `--run`.

### Isolated CN027182 lookup test

To move past CN027182's missing invoice association, run the bounded read-only
candidate search in the configured terminal:

```powershell
.\.venv\Scripts\python.exe find_creditnote_candidate.py --limit 50
```

This excludes CN027182, reads at most 50 mapped credit notes, and stops at the
first non-void document with a numeric existing `invoice_id`, an invoice number,
and an unpopulated Customer Branch representation. Applied credits alone do
not establish that association. A candidate still requires a guarded test;
it is not proof that Zoho will accept an update. Local report, candidate and raw
snapshot files go into a timestamped `creditnote_candidate_scan_*` directory.
Progress prints every 10 records. No document writes, bulk status changes or
halt changes occur. If needed, `--skip 50 --limit 50` examines the next sample.

`test_creditnote_branch.py` is restricted to credit note CN027182
(`1462401000008001966`). It uses the operator-captured edit-page response in
`output/corrected_run/creditnote_CN027182_review.json`, checks its fixed SHA-256,
and obtains document index **1** from that response rather than settings index 2.
The operator confirmed the branch blank in the UI. Fresh field settings must
still identify the active, writable lookup, and a fresh GET must match the
reviewed document/customer and have no populated branch representation.

Run from the project directory in the PowerShell session with Zoho settings:

```powershell
.\.venv\Scripts\python.exe test_creditnote_branch.py --apply
```

Omit `--apply` for a read-only preview. The apply command saves a fresh original
snapshot, checks again for concurrent changes, then attempts exactly one
document-scoped custom-fields POST. It does not create a credit note, fall back
to a full-document update, or request additional OAuth scopes. The existing
scope discrepancy described above may cause rejection; retain the response
evidence rather than retrying or broadening access automatically.

Before/after documents, request outcome and verification results stay under
`output/corrected_run/creditnote_CN027182_test`. A durable attempt marker blocks
subsequent sends, including after a timeout or crash. Do not delete it to retry.
For the investigated first attempt that stopped with "Write authentication
expired; reconcile before retry", `--retry-after-auth --apply` permits one
separately recorded attempt after credentials are replaced. It validates the
old mapping, payload, authentication error and unchanged blank readback, then
requires fresh business data to match the previous original. Evidence is kept
in `creditnote_CN027182_auth_retry`; its own attempt marker prevents resend.
After the recorded index rejection (HTTP 400, code 120121), the explicitly
selected `--test-field-id --apply` mode tests a branch-only
`PUT /creditnotes/1462401000008001966` with `customfield_id` rather than index.
This field-ID representation was captured from the browser save payload; the
browser's full-document save itself failed for missing Associated Invoice Number
(12069), so partial-update acceptance is not established until this test passes.
The script sends no invoice association, customer, line items, totals or other
custom fields and never falls back to resubmitting the browser document. It
requires the saved index-rejection evidence and unchanged fresh business data.
New evidence stays in `creditnote_CN027182_field_id_test`, whose durable marker
allows only one send. Any rejection requires investigation, not repeated saves.
Verification compares all returned business fields and other custom fields,
excluding only the target branch representations and modification audit fields.
Post-write branch absence cannot pass. This isolated test never modifies the
bulk SQLite database, clears its halt, or counts itself as bulk test approval.

```powershell
python backfill_customer_branch.py --dry-run
```

Reads mapped documents without remote writes. Outcomes are `ready`,
`already_correct`, `conflict` or `failed`. Review `output/report.csv`,
`summary.json` and `backfill.log`. Conflicts are left unchanged. Ready rows are
counted as pending in the reconciliation summary; CSV `detail` retains `ready`.
Completed and conflicting rows are skipped on later runs. Dry runs after write
attempts also reconcile saved original snapshots and may halt on discrepancies.

## Live tests and full run

### Reviewing lookups omitted from GET responses

If the lookup is absent from `custom_fields`, the ordinary dry run fails closed.
After inspecting its field metadata, use this **read-only** review mode:

```powershell
python backfill_customer_branch.py --dry-run --review-omitted-lookups --csv data/Armco_Branch_Backfill_API_Map_corrected.csv --output output/corrected_run --max-failures 1
```

This requires settings.READ and independently verifies both configured IDs against
active, editable, visible lookup metadata for the correct entity/API name and
Customer Branch module. It saves that metadata locally. A missing entry is
classified as **`ready_omitted`**, not confirmed empty, only if `custom_fields`
and `custom_field_hash` are valid containers and no populated target mirror or
conflicting API-name entry is present. These provisional rows count as pending
and include an explanation in `report.csv`. Existing nonempty lookups still
become already-correct or conflict results as usual.

The option is rejected with test/run modes and cannot enable writes. Do not
interpret provisional candidates as proof that Zoho returns every populated
lookup. Controlled live representation testing remains necessary; ordinary
updates continue to reject omitted fields. Original snapshots from any previous
write attempt are always reconciled rather than bypassed by this review option.

For a single controlled **invoice** test whose branch is omitted from GET, first
open that invoice in Zoho, confirm Customer Branch is actually blank, and cancel
without saving. Use `--type invoice --test 1 --document-id ACTUAL_ID` plus
`--confirm-empty-in-ui --review-note "Operator checked branch blank in Zoho"`,
the corrected `--csv` and the existing `--output`. This option rechecks the lookup
schema, records the operator's confirmation and permits only that one explicit
invoice test. It saves the actual original GET response without inserting a fake
custom-field entry. A populated branch must appear in read-back with the expected
record ID, and every protected field must remain unchanged. Any discrepancy
halts processing persistently. An absent post-update field cannot pass verification.
Existing populated values and conflicting mirrors are still checked before writing.

This option does not enable omitted-field bulk processing or credit-note tests.
Invoice writes address the stable `customfield_id`. Observed document index 1
versus settings index 2 means credit-note index payloads still need independent
contract confirmation; do not assume those indexes are interchangeable. Existing
per-module live verification and manual-approval gates remain in force.

After reviewing the dry run, choose one mapped invoice and one mapped credit
note with an empty branch. Explicitly select them:

```powershell
python backfill_customer_branch.py --type invoice --test 1 --document-id INVOICE_ID
python backfill_customer_branch.py --type creditnote --test 1 --document-id CREDITNOTE_ID
```

These commands write to Zoho. Without optional `--document-id`, each attempts
the first unfinished row for that module. At most one document is attempted.
Already-correct or conflicting rows do not pass the changed-and-verified test
gate; select another mapped empty-branch document if necessary.

Check each document in Zoho Books: Customer Branch, customer, lines, totals,
payments, status, fiscal information, other custom fields and the Customer Branch
related list. Retain local evidence. Only after UI inspection record approval:

```powershell
python backfill_customer_branch.py --approve-test invoice --review-note "Reviewer and local evidence reference"
python backfill_customer_branch.py --approve-test creditnote --review-note "Reviewer and local evidence reference"
python backfill_customer_branch.py --run
```

The full run requires both changed-and-API-verified tests and both manual
approvals in the same database. Verified, already-correct and conflicting rows
are skipped; failed, ready and pending rows are read again. Incomplete writes
are reconciled before a new write. A finished command can still have conflicts
or failures: inspect reports to determine completion. A test that does not
verify a change exits with an error.

## Verification, restart and recovery

Before each write, the full original GET response and `updating` state are
durably committed to SQLite. Only the branch field is submitted. A new GET must
confirm the branch and unchanged protected data before marking completion.
Write responses or rejected-write details and the first read-back response are
also saved as timestamped `evidence:*` entries in SQLite metadata. These may
contain customer data and vendor error messages; they are retained locally and
not printed to the console. If read-back comparison fails, the preceding write
outcome remains available, rather than being obscured by the verification error.
This evidence capture applies to future attempts; it cannot recover a response
discarded by an earlier version of the script.
The comparison covers all returned document data, including customer, lines,
totals, payments, status and fiscal custom fields. It excludes only top-level
`last_modified_time`/`updated_time` and the target branch's custom-field entry,
API-name mirror and hash mirror. Other metadata changes conservatively stop the
run. Data absent from the GET response cannot be verified by this comparison;
UI and related-list checks remain necessary. There is no atomic conditional
read/write transaction: use a quiet window to minimise concurrent changes.

Unexpected protected-data or branch verification differences save a persistent
`halted` flag, mark the row failed and stop further work. Restarts retain the
halt. Do not delete the DB or simply bypass the flag. Back up the entire output
folder, compare the original snapshot to a fresh GET and UI evidence, and
investigate concurrent changes/API behaviour. Have the owner review a targeted
repair and evidence before deliberately clearing the flag using an inspected
SQLite maintenance procedure. No automatic unhalt or bulk rollback is supplied.

After a failed live test, audit the selected document without retrying the write:

```powershell
python backfill_customer_branch.py --audit-document ACTUAL_ID --type invoice --csv data/Armco_Branch_Backfill_API_Map_corrected.csv --output output/corrected_run
```

This uses the existing run identity and original snapshot from a read-only SQLite
connection, performs one fresh document GET (plus OAuth/retries if needed), and
saves `original.json`, `current.json` and `audit.json` in a new `audit_*` folder.
Console output includes changed JSON paths and whether the expected branch is
present; full values stay in local evidence files. It does not mark completion,
retry writes, relax comparisons or clear the halt. A fresh read cannot reconstruct
the exact response observed at the initial failure, so investigate concurrent
edits as well as write effects. Use `--type creditnote` for credit-note audits.

Writes are never blindly retried by the HTTP transport. After an uncertain
write response, a GET checks whether it succeeded. Verified read-back can mark
success even if the response was lost. If not confirmed, the row stays failed
with its original snapshot; restart reads and compares before retrying. A crash
after writing but before verification follows the same reconciliation path.

For ordinary interruption, rerun the same command with the same CSV, settings
and output directory. An OS lock prevents simultaneous processes sharing that
output directory and releases automatically after a crash. Never run the same
map concurrently using different output directories. Retain and locally back
up the database, which holds original values and approvals. Do not edit CSV rows
to resolve conflicts during an existing run.

For targeted reversal use the saved document's `original` JSON in SQLite. First
check the current branch and protected data; restore the **recorded previous
value**, not a blanket empty branch. Review the individual payload, test the
actual Zoho lookup-clearing contract if relevant, then GET and compare again.
Retain before/after evidence. Automatic reversal is outside this tool's scope.

## Rate limits and runtime

For the observed browser save rejection `1070673` (Cash Customer: add cash
customer), inspect field metadata without editing the invoice or rule:

```powershell
.\.venv\Scripts\python.exe inspect_cash_customer.py
```

This uses settings.READ and only `GET /settings/fields?entity=invoice`. It prints
the Cash Customer field's mandatory/permission flags and the names of any
validation metadata keys. Full metadata is saved locally under
`output/cash_customer_inspection/`. It cannot prove the full conditions of the
blocking rule: an optional field can still be required by a conditional rule.
A documented Books API for listing those rule conditions was not identified.
Use the settings UI or obtain the rule details from its owner if they are absent
from this metadata. The command never guesses Cash Customer values, disables
rules, retries invoice writes or clears the saved halt.

With `--probe-validation-rules`, this diagnostic also tries **GET only** on
`/settings/validationrules?entity=invoice`. This is an exploratory route, not a
publicly confirmed Books API contract. It saves the response locally and prints
the status and matching rule identifiers if a recognised list is returned. A
404/401 or unknown response shape does not establish that no rule exists. No
write endpoint is inferred from a successful read; inspect the actual rule and
prepare the targeted change before modifying settings.

For faster read-only processing, `--delay 0 --requests-per-minute 85` removes the
fixed pre-request pause. Request starts are spaced at least 60/85 seconds apart,
including OAuth, renewals and retries; time spent waiting for Zoho counts toward
that interval. This cap applies to one client process, not other integrations.
The supported target range is 1..90 requests/minute to retain some headroom below
Zoho's organisation-wide limit. Backoff and daily quota handling still apply.

To apply this to a running dry run, press Ctrl+C in its terminal, keep that same
PowerShell session open, then restart with the same CSV/output and add
`--delay 0 --requests-per-minute 85 --resume-dry-run`. The resume option skips
saved `ready` rows and, with fresh schema checks enabled, `ready_omitted` rows;
failed and interrupted rows are retried. It is accepted only with `--dry-run`.
This reuses earlier review evidence rather than refreshing it. Omit the option
when an intentional full reread is required. Never start a second concurrent run.
The separate monitor continues reading the same database after restart.

Requests use 10-second connect and 60-second read timeouts, access-token renewal,
bounded read/refresh retries, exponential backoff, numeric `Retry-After`, and a
configurable delay before every request. Redirects are disabled; credentials
are sent only to matching official HTTPS data-centre hosts. Tokens, secrets and
API error bodies are not logged. Quota waits over 300 seconds stop for a later
restart. Options: `--delay 0.7`, `--retries 3` additional attempts and
`--max-failures 5` consecutive document failures. Verification differences stop
immediately regardless of that failure threshold.

[Zoho's documented limits](https://www.zoho.com/books/api/v3/introduction/#api-call-limit)
currently list 100 requests/minute per organisation and daily plan quotas from
1,000 to 10,000 calls. Check this organisation's actual allowance and competing
usage. A dry run takes about 16,902 GETs. Updating every row takes about **50,706
calls** (GET + write + GET), plus discovery, OAuth, tests and retries. At 100 calls
per minute the write phase has an ideal lower bound of **8.45 hours** before
latency/backoff; daily quotas may spread it over several days. Default delay
limits this process to fewer than 86 requests/minute before latency and cannot
reserve capacity from other clients. Do not use the earlier three-hour estimate.
Run outside busy invoicing hours and keep the computer awake and connected.

## Reports, spot checks and Collins handoff

For automatic local dry-run progress checks every five minutes, run this in a
second terminal while leaving the dry run running:

```powershell
.\.venv\Scripts\python.exe watch_progress.py --output output/corrected_run
```

Open `output/corrected_run/monitor/progress.md` in VS Code for the latest status.
`progress.json` and `history.jsonl` hold machine-readable snapshots. The monitor
also refreshes `monitor/report.csv` and `monitor/summary.json` every interval,
using the same read-only snapshot as its progress counts. These are interim
reports; the dry run's root-level final reports remain separate. Keep CSVs closed
in applications that lock files while the monitor writes them.
The monitor
only reads SQLite, uses no credentials and makes no Zoho calls. Its own lock
prevents duplicate monitors in that folder. It stops after all rows are reviewed
or failures/a halt are recorded, and flags two intervals with no progress.
`--once` prints one snapshot. Ctrl+C stops only the monitor, not the dry run.
These are local file updates, not scheduled chat notifications. Provisional
rows count as reviewed but never as completed remote updates.

```powershell
python backfill_customer_branch.py --report
```

Regenerates reports without remote calls; matching settings/map are still
required. Reports are also written on completion, processing errors and Ctrl+C.
These mutually exclusive categories reconcile to `total_mapped`:

| Category | Meaning |
| --- | --- |
| `updated_verified` | Changed and verified against the saved original |
| `already_correct` | Expected branch found before any tool write |
| `conflict` | Different branch found; left unchanged |
| `failed` | Could not complete read, update or verification |
| `pending` | Unprocessed, dry-run ready or interrupted during a write |

The CSV includes document IDs, numbers, branches and failure details. The
database retains full original snapshots; logs record document outcomes.
Restrict access to and back up the entire output folder as customer data.
Potential spreadsheet formula strings are prefixed with an apostrophe during
CSV export; original IDs remain intact in SQLite.

Spot-check **five invoices and two credit notes at minimum across the named
parent customers**, including Customer Branch related lists. Parent names and
the map were not supplied here. Keep selected IDs, parent names, reviewer, date
and findings in local evidence. Provide Collins the final report, logs and
verification evidence through an approved channel. **Customer merges must wait
until Collins confirms verification.** Sending that handoff and merging
customers are not automated.

## Completion checklist

- [ ] GitHub repository confirmed private; source reviewed and pushed
- [x] Local CLI, durable progress, tests and operating instructions implemented
- [ ] Approved CSV received, reviewed, fingerprinted and validated
- [ ] Organisation and data centre independently confirmed
- [ ] Both branch field IDs discovered; credit-note index confirmed
- [ ] Lookup payload and credit-note scope discrepancy resolved live
- [ ] Dry run reviewed
- [ ] One invoice and one credit note changed, API-verified and UI-checked
- [ ] Both manual approvals recorded
- [ ] Full backfill completed; all 16,902 records reconciled
- [ ] At least five invoices and two credit notes checked across named parents
- [ ] Collins received evidence and confirmed verification before any merges
