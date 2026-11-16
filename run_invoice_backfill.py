"""Invoice-only field-ID backfill with a separate durable journal and readback checks."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from backfill_customer_branch import (BackfillError, Client, Store, branch_value,
    document_differences, dumps, find_field, load_map, run_lock, settings,
    validate_lookup_schema)
from repair_test_invoice import projection


def lookup(client, row, field_id):
    body = client.request("GET", "/invoices/" + row["zoho_id"] + "/customfields")
    doc = {"custom_fields": body.get("custom_fields")}
    field = find_field(doc, field_id)
    if field.get("api_name") != "cf_customer_branch" or field.get("data_type") != "lookup":
        raise BackfillError("Fresh invoice lookup endpoint has unexpected field metadata")
    return branch_value(doc, field_id), body


def update_one(client, store, row, schema, test=False):
    fid = schema["field_id"]
    saved = store.db.execute("SELECT * FROM documents WHERE type=? AND zoho_id=?",
                             (row["type"], row["zoho_id"])).fetchone()
    if saved["original"]:
        # Never automatically resend an attempted write, even after process termination.
        raise BackfillError("Saved attempt requires reconciliation before retry")
    before = client.read(row)
    current, fields = lookup(client, row, fid)
    present = [f for f in before.get("custom_fields", []) if str(f.get("customfield_id")) == fid]
    if present and branch_value(before, fid) != current:
        raise BackfillError("Invoice GET and custom-fields endpoint disagree")
    if current:
        store.update(row, "already_correct" if current == row["branch_record_id"] else "conflict")
        return False
    # Ensures omitted mirrors are blank, never changes the saved original document.
    if branch_value(before, fid, schema) != "":
        raise BackfillError("Invoice branch is not empty")
    for container in (before, before.get("custom_field_hash", {})):
        if container.get("cf_customer_branch_unformatted") not in (None, ""):
            raise BackfillError("Unformatted branch mirror is populated")
    # Save evidence transactionally before the only write.
    store.update(row, "updating", original=before)
    store.record_evidence(row, "lookup_before", fields)
    payload = {"custom_fields": [{"customfield_id": fid, "value": row["branch_record_id"]}]}
    store.record_evidence(row, "request", {"method": "PUT", "path": "/invoices/" + row["zoho_id"],
                                           "payload": payload})
    try:
        response = client.request("PUT", "/invoices/" + row["zoho_id"], payload)
        store.record_evidence(row, "write_response", response)
    except BackfillError as error:
        store.record_evidence(row, "write_error", {"error": str(error),
            "http_status": getattr(error, "status", None), "response": getattr(error, "body", None)})
    after = client.read(row)
    store.record_evidence(row, "readback", after)
    changes = document_differences(projection(before, fid, schema), projection(after, fid, schema))
    if changes:
        store.record_evidence(row, "business_differences", changes)
        raise BackfillError("Other business details changed; stopped for investigation")
    # A populated full document response is required; an omitted post-write field cannot pass.
    if branch_value(after, fid) != row["branch_record_id"]:
        raise BackfillError("Customer Branch update was not verified")
    store.update(row, "updated_verified", test=test)
    return True


def reconcile_read_failure(client, store, schema, folder):
    if store.get("halted") != "Read failed after bounded retries":
        raise BackfillError("Recovery is restricted to the investigated read-retry halt")
    failed = list(store.db.execute("SELECT * FROM documents WHERE status IN ('failed','updating')"))
    if len(failed) != 1 or not failed[0]["original"]:
        raise BackfillError("Expected exactly one saved attempted invoice")
    saved = failed[0]
    row, before = json.loads(saved["mapping"]), json.loads(saved["original"])
    evidence = list(store.db.execute("SELECT key,value FROM meta WHERE key LIKE ?",
                                    ("evidence:invoice:" + row["zoho_id"] + ":%",)))
    responses = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":write_response")]
    requests = [json.loads(item["value"]) for item in evidence if item["key"].endswith(":request")]
    expected = {"method": "PUT", "path": "/invoices/" + row["zoho_id"], "payload": {
        "custom_fields": [{"customfield_id": schema["field_id"], "value": row["branch_record_id"]}]}}
    if (len(responses) != 1 or responses[0].get("code") != 0 or requests != [expected] or
            any(item["key"].endswith(":write_error") for item in evidence)):
        raise BackfillError("Saved successful write evidence is missing or ambiguous")
    after = client.read(row)
    store.record_evidence(row, "recovery_readback", after)
    if (projection(before, schema["field_id"], schema) != projection(after, schema["field_id"], schema) or
            branch_value(after, schema["field_id"]) != row["branch_record_id"]):
        raise BackfillError("Recovery read does not verify; halt retained and no write sent")
    backup = folder / ("before_recovery_" + str(time.time_ns()) + ".sqlite3")
    with contextlib.closing(sqlite3.connect(backup)) as destination:
        store.db.backup(destination)
    with store.db:
        store.db.execute("UPDATE documents SET status='updated_verified',error='' WHERE type='invoice' AND zoho_id=?",
                         (row["zoho_id"],))
        store.put("halted", None)
        store.put("read_failure_recovery:" + str(time.time_ns()), {
            "number": row["number"], "document_id": row["zoho_id"], "backup": str(backup),
            "verified_by_fresh_read": True, "remote_updates_performed": 0})
    print(dumps({"recovered_invoice": row["number"], "verified": True,
                 "remote_updates_performed": 0, "invoice_run_halt_cleared": True}), flush=True)


def reconcile_interrupted(client, store, schema):
    """Read-only reconciliation after Ctrl+C; never resend an uncertain request."""
    for saved in list(store.db.execute("SELECT * FROM documents WHERE status='updating'")):
        row, before = json.loads(saved["mapping"]), json.loads(saved["original"])
        after = client.read(row)
        store.record_evidence(row, "interruption_readback", after)
        if projection(before, schema["field_id"], schema) != projection(after, schema["field_id"], schema):
            raise BackfillError("Interrupted invoice has business differences; no write repeated")
        value = branch_value(after, schema["field_id"], schema)
        if value == row["branch_record_id"]:
            # Require actual populated field, not omission fallback.
            branch_value(after, schema["field_id"])
            store.update(row, "updated_verified")
        else:
            request = store.db.execute("SELECT 1 FROM meta WHERE key LIKE ?",
                ("evidence:invoice:" + row["zoho_id"] + ":%:request",)).fetchone()
            if value or request:
                raise BackfillError("Interrupted request is uncertain; no automatic resend permitted")
            # The request journal is always committed before the network call.
            store.record_evidence(row, "interruption_original", before)
            with store.db:
                store.db.execute("UPDATE documents SET status='pending',original=NULL WHERE type='invoice' AND zoho_id=?",
                                 (row["zoho_id"],))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test", action="store_true", help="Stop after one successful changed invoice")
    mode.add_argument("--run", action="store_true", help="Resume invoices after the isolated test succeeds")
    mode.add_argument("--reconcile-read-failure", action="store_true",
                      help="Read and verify the single successful write halted by exhausted read retries")
    parser.add_argument("--limit", type=int, default=14450)
    parser.add_argument("--requests-per-minute", type=int, default=90,
                        help="Request cap 1..90; default 90 (previously 85)")
    args = parser.parse_args()
    if not 1 <= args.limit <= 14450:
        raise BackfillError("Limit must be 1..14450")
    if not 1 <= args.requests_per_minute <= 90:
        raise BackfillError("Request cap must be 1..90 per minute")
    rows, fingerprint = load_map("data/Armco_Branch_Backfill_API_Map_corrected.csv")
    cfg = settings(["invoice"])
    if cfg["ZOHO_ORG_ID"] != "941141787" or cfg["CF_INVOICE_ID"] != "1462401000020819910":
        raise BackfillError("Invoice settings differ from approved organisation/field")
    root = Path("output/corrected_run")
    with run_lock(root):
        # Investigated old halt remains untouched. Require its repaired manual test
        # invoice to still match original business data before creating the new journal.
        with contextlib.closing(sqlite3.connect((root / "progress.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as db:
            identity = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
            old = db.execute("SELECT mapping,original FROM documents WHERE type='invoice' AND zoho_id='1462401000001974003'").fetchone()
        for key in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API", "CF_INVOICE_ID"):
            if cfg[key] != identity.get(key):
                raise BackfillError("Settings differ from original invoice run")
        if fingerprint not in identity.values() or not old or not old[1]:
            raise BackfillError("Original approved map/snapshot missing")
        client = Client(cfg, delay=0, requests_per_minute=args.requests_per_minute)
        schema = validate_lookup_schema(client.request("GET", "/settings/fields", query={"entity": "invoice"}),
                                        cfg["CF_INVOICE_ID"], "invoice")
        old_row, original = json.loads(old[0]), json.loads(old[1])
        repaired = client.read(old_row)
        if (projection(original, cfg["CF_INVOICE_ID"], schema) != projection(repaired, cfg["CF_INVOICE_ID"], schema) or
                branch_value(repaired, cfg["CF_INVOICE_ID"]) != old_row["branch_record_id"]):
            raise BackfillError("Investigated invoice repair does not verify; original halt retained")
        folder = root / "invoice_field_id_run"
        folder.mkdir(exist_ok=True)
        invoices = [r for r in rows if r["type"] == "invoice"]
        store = Store(folder / "progress.sqlite3", invoices,
                      {**{k: cfg[k] for k in ("ZOHO_ORG_ID", "ZOHO_ACCOUNTS", "ZOHO_API", "CF_INVOICE_ID")},
                       "map_sha256": fingerprint, "protocol": "invoice-field-id-v1"})
        try:
            if args.reconcile_read_failure:
                reconcile_read_failure(client, store, schema, folder)
                print(dumps(store.report(folder)), flush=True)
                return
            if store.get("halted"):
                raise BackfillError("Invoice-only run halted; inspect saved evidence before continuing")
            reconcile_interrupted(client, store, schema)
            verified = store.db.execute("SELECT count(*) FROM documents WHERE test_verified=1 AND status='updated_verified'").fetchone()[0]
            if args.run and not verified:
                raise BackfillError("One changed invoice must pass --test before --run")
            if args.test and verified:
                print("Invoice test already passed; no further test write sent.")
                return
            checked = 0
            for row in invoices:
                saved = store.db.execute("SELECT status FROM documents WHERE type='invoice' AND zoho_id=?", (row["zoho_id"],)).fetchone()
                if saved[0] in ("updated_verified", "already_correct", "conflict"):
                    continue
                if checked >= args.limit:
                    break
                checked += 1
                try:
                    changed = update_one(client, store, row, schema, args.test)
                except BackfillError as error:
                    store.update(row, "failed", str(error))
                    store.halt(str(error))
                    raise
                if checked % 10 == 0 or args.test:
                    print(dumps(store.report(folder)), flush=True)
                if args.test and changed:
                    break
            print(dumps(store.report(folder)), flush=True)
        finally:
            store.report(folder)
            store.db.close()


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError, ValueError, sqlite3.Error) as error:
        print("Invoice run stopped: " + str(error), file=sys.stderr)
        if getattr(error, "status", None) is not None:
            diagnostic = Path("output/corrected_run/invoice_field_id_run") / ("access_error_" + str(time.time_ns()) + ".json")
            try:
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(dumps({"http_status": error.status,
                    "response": getattr(error, "body", None)}) + "\n", encoding="utf-8")
                print("API error evidence: " + str(diagnostic), file=sys.stderr)
            except OSError:
                pass
        sys.exit(1)
