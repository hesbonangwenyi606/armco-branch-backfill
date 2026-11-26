"""Bounded credit-note backfill with durable evidence and stop-on-error guards."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

from backfill_customer_branch import (APIError, BackfillError, Client, branch_value,
    document_differences, dumps, load_map, protected, run_lock, settings, validate_lookup_schema)
from test_creditnote_cn027250 import field_value

FIELD = "1462401000020819915"
ROOT = Path("output/corrected_run/creditnote_batch")
INPUT = Path("output/corrected_run/creditnote_preliminary_candidates.csv")


def normalized(doc, schema):
    value = protected(doc, FIELD, schema)
    for key in ("invoice_id", "invoice_number", "reference_invoice_ids", "last_modified_by_id"):
        value.pop(key, None)
    for container in (value, value.get("custom_field_hash", {})):
        if isinstance(container, dict):
            for key in ("cf_customer_branch_unformatted", "cf_customer_branch_secondary_value_formatted"):
                container.pop(key, None)
    return value


def verify(before, after, allocations, after_allocations, fields, cn, inv, schema):
    if after.get("invoice_id") != inv["zoho_id"] or after.get("invoice_number") != inv["number"]:
        raise BackfillError("Invoice association was not verified")
    if after.get("reference_invoice_ids") not in (None, "", [], [inv["zoho_id"]]):
        raise BackfillError("Unexpected invoice association representation")
    if allocations != after_allocations:
        raise BackfillError("Invoice allocations changed")
    if field_value(fields) != cn["branch_record_id"] or branch_value(after, FIELD, schema) != cn["branch_record_id"]:
        raise BackfillError("Branch was not verified in both responses")
    for container in (after, after.get("custom_field_hash", {})):
        if isinstance(container, dict):
            for key in ("cf_customer_branch_unformatted",):
                if container.get(key) not in (None, "", cn["branch_record_id"]):
                    raise BackfillError("Conflicting branch ID mirror")
            for key in ("cf_customer_branch_secondary_value_formatted",):
                if container.get(key) not in (None, "", cn["branch_name"]):
                    raise BackfillError("Conflicting branch name mirror")
    changes = document_differences(normalized(before, schema), normalized(after, schema))
    if changes:
        raise BackfillError("Protected data changed: " + ", ".join(d["path"] for d in changes[:20]))


def progress(db, total):
    counts = dict(db.execute("SELECT status, COUNT(*) FROM attempts GROUP BY status"))
    lines = ["# Credit-note batch progress", "", "Checked (UTC): " + datetime.now(timezone.utc).isoformat(), "",
             "Candidate list size: " + str(total), "", "Verified updates: " + str(counts.get("verified", 0)),
             "Already present: " + str(counts.get("already_present", 0)),
             "Attempted or failed: " + str(counts.get("attempting", 0) + counts.get("failed", 0) + counts.get("isolated", 0)), "",
             "This report covers the candidate list only, not all mapped credit notes.", ""]
    (ROOT / "progress.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1, help="Maximum candidates checked in this invocation")
    parser.add_argument("--continue-all", action="store_true", help="Continue through all remaining candidates, stopping on any error")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        raise BackfillError("Use --limit between 1 and 100")
    rows, fingerprint = load_map("data/Armco_Branch_Backfill_API_Map_corrected.csv")
    raw = INPUT.read_bytes()
    with INPUT.open(encoding="utf-8-sig", newline="") as f:
        candidates = list(csv.DictReader(f))
    by_id = {(r["type"], r["zoho_id"]): r for r in rows}
    if len({r["creditnote_id"] for r in candidates}) != len(candidates):
        raise BackfillError("Duplicate candidate credit notes")
    for c in candidates:
        cn = by_id.get(("creditnote", c["creditnote_id"]))
        inv = by_id.get(("invoice", c["invoice_id"]))
        if (not cn or not inv or cn["number"] != c["creditnote_number"] or inv["number"] != c["applied_invoice_number"]
                or cn["branch_record_id"] != c["branch_record_id"] or cn["branch_name"] != c["branch_name"]):
            raise BackfillError("Candidate list differs from the approved mapping")
    cfg = settings(["creditnote", "invoice"])
    if cfg["ZOHO_ORG_ID"] != "941141787" or cfg["CF_CREDITNOTE_ID"] != FIELD:
        raise BackfillError("Organisation or field differs from approved configuration")
    ROOT.mkdir(parents=True, exist_ok=True)
    with run_lock(ROOT), sqlite3.connect(ROOT / "progress.sqlite3", timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, number TEXT, status TEXT NOT NULL, evidence TEXT NOT NULL)")
        identity = dumps({"map_sha256": fingerprint, "candidates_sha256": hashlib.sha256(raw).hexdigest(),
                          "organisation": cfg["ZOHO_ORG_ID"], "api": cfg["ZOHO_API"], "field": FIELD})
        stored = db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if stored and stored[0] != identity:
            raise BackfillError("Journal identity does not match map, candidate list or configuration")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('identity', ?)", (identity,))
        db.commit()
        if db.execute("SELECT 1 FROM attempts WHERE status IN ('attempting','failed') LIMIT 1").fetchone():
            raise BackfillError("Unresolved attempted or failed record; inspect the journal before proceeding")
        isolated = db.execute("SELECT number, evidence FROM attempts WHERE status='isolated'").fetchall()
        for number, raw_evidence in isolated:
            item = json.loads(raw_evidence)
            if number != 'CN030192' or item.get('isolation') != 'CN030192_401_code57':
                raise BackfillError('Unexpected isolated credit note; inspect the journal before proceeding')
        # Match the existing 90 requests/minute ceiling used by the invoice runner.
        client = Client(cfg, delay=60 / 90, requests_per_minute=90)
        schema = validate_lookup_schema(client.request("GET", "/settings/fields", query={"entity": "creditnote"}), FIELD, "creditnote")
        progress(db, len(candidates))
        processed = 0
        for c in candidates:
            if not args.continue_all and processed >= args.limit:
                break
            if db.execute("SELECT 1 FROM attempts WHERE id=?", (c["creditnote_id"],)).fetchone():
                continue
            cn = by_id[("creditnote", c["creditnote_id"])]
            inv = by_id[("invoice", c["invoice_id"])]
            evidence = {"mapping": cn, "invoice_mapping": inv}
            try:
                before = client.read(cn)
                invoice = client.read(inv)
                allocations = client.request("GET", "/creditnotes/" + cn["zoho_id"] + "/invoices")
                fields = client.request("GET", "/creditnotes/" + cn["zoho_id"] + "/customfields")
                evidence.update({"before": before, "invoice_before": invoice, "allocations_before": allocations, "fields_before": fields})
                value = field_value(fields)
                credited = allocations.get("invoices_credited")
                if (not before.get("customer_id") or before.get("customer_id") != invoice.get("customer_id")
                        or not isinstance(credited, list) or len(credited) != 1 or credited[0].get("invoice_id") != inv["zoho_id"]
                        or credited[0].get("invoice_number") != inv["number"]):
                    raise BackfillError("Customer or allocation mismatch")
                if (value == cn["branch_record_id"] and branch_value(before, FIELD, schema) == cn["branch_record_id"]
                        and before.get("invoice_id") == inv["zoho_id"] and before.get("invoice_number") == inv["number"]):
                    db.execute("INSERT INTO attempts VALUES (?, ?, 'already_present', ?)", (cn["zoho_id"], cn["number"], dumps(evidence)))
                    db.commit()
                else:
                    if (value != "" or branch_value(before, FIELD, schema) != "" or before.get("invoice_id") not in (None, "")
                            or before.get("invoice_number") not in (None, "") or before.get("reference_invoice_ids") not in (None, "", [])):
                        raise BackfillError("Branch or association is populated or ambiguous")
                    payload = {"invoice_id": inv["zoho_id"], "custom_fields": [{"customfield_id": FIELD, "value": cn["branch_record_id"]}]}
                    evidence["request"] = payload
                    db.execute("INSERT INTO attempts VALUES (?, ?, 'attempting', ?)", (cn["zoho_id"], cn["number"], dumps(evidence)))
                    db.commit()
                    evidence["write_response"] = client.request("PUT", "/creditnotes/" + cn["zoho_id"], payload)
                    db.execute("UPDATE attempts SET evidence=? WHERE id=?", (dumps(evidence), cn["zoho_id"]))
                    db.commit()
                    after = client.read(cn)
                    after_allocations = client.request("GET", "/creditnotes/" + cn["zoho_id"] + "/invoices")
                    after_fields = client.request("GET", "/creditnotes/" + cn["zoho_id"] + "/customfields")
                    evidence.update({"after": after, "allocations_after": after_allocations, "fields_after": after_fields})
                    verify(before, after, allocations, after_allocations, after_fields, cn, inv, schema)
                    db.execute("UPDATE attempts SET status='verified', evidence=? WHERE id=?", (dumps(evidence), cn["zoho_id"]))
                    db.commit()
                processed += 1
                progress(db, len(candidates))
                print(dumps({"number": cn["number"], "checked_in_batch": processed, "limit": "all" if args.continue_all else args.limit}), flush=True)
            except (BackfillError, OSError, ValueError) as exc:
                evidence["error"] = str(exc)
                if isinstance(exc, APIError):
                    evidence.update({"http_status": exc.status, "response": exc.body})
                db.execute("INSERT INTO attempts VALUES (?, ?, 'failed', ?) ON CONFLICT(id) DO UPDATE SET status='failed', evidence=excluded.evidence",
                           (cn["zoho_id"], cn["number"], dumps(evidence)))
                db.commit()
                progress(db, len(candidates))
                raise BackfillError("Stopped at " + cn["number"] + ": " + str(exc)) from None
        print(dumps({"checked_in_batch": processed, "progress": str(ROOT / "progress.md")}))


if __name__ == "__main__":
    try:
        main()
    except (BackfillError, OSError, ValueError, sqlite3.Error) as exc:
        print("Credit-note batch stopped: " + str(exc), file=sys.stderr)
        sys.exit(1)
